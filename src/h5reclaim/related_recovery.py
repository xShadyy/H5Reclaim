"""Expand explicitly pinned external object trees into a local recovery graph."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from .recovery import RecoveryError, _verify_source


class RelatedRecovery:
    def __init__(self, manifest, budget, directory):
        from .dependency_routes import load_dependency_manifest
        self.entries = {item['declared_name']: item for item in load_dependency_manifest(manifest)['files']}
        self.budget, self.directory = budget, Path(directory)
        self.stack = ExitStack()
        self.captures = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def capture(self, declared_name):
        from .large_streaming import sparse_snapshot
        if declared_name in self.captures:
            return self.captures[declared_name]
        if declared_name not in self.entries:
            raise RecoveryError(f'external file was not supplied: {declared_name}')
        entry = self.entries[declared_name]
        source = Path(entry['path'])
        image, digest, identity, size, copied = self.stack.enter_context(sparse_snapshot(source, budget=self.budget))
        if digest != entry['sha256']:
            raise RecoveryError('external file differs from its pinned SHA-256')
        record = dict(source=source, image=image, digest=digest, identity=identity, size=size, copied=copied)
        self.captures[declared_name] = record
        return record

    def expand(self, inventory, main_digest):
        from .whole_file import _inventory
        from .source_session import share_image
        sections = ('groups', 'datasets', 'named_types', 'soft_links', 'group_aliases')
        for section in ('groups', 'datasets', 'named_types'):
            for entry in inventory.get(section, []):
                entry['storage_digest'] = main_digest
        pending = list(inventory.get('external_links', []))
        inventory['external_links'] = []
        seen = {(main_digest, entry['address']): entry['path']
                for entry in inventory['groups'] if 'address' in entry}
        while pending:
            link = pending.pop(0)
            try:
                capture = self.capture(link['filename'])
                with share_image(capture['image'], capture['digest'], capture['size'], capture['copied'], self.budget):
                    child = _inventory(capture['image'], self.directory, root=link['target'], budget=self.budget)
                source_root = link['target'].rstrip('/') or '/'
                target_root = link['path']
                def mapped(path):
                    return target_root + path[len(source_root):] if source_root != '/' else target_root + (path if path != '/' else '')
                root_group = next((entry for entry in child['groups'] if entry['path'] == source_root), None)
                if root_group:
                    identity = (capture['digest'], root_group['address'])
                    if identity in seen:
                        inventory['group_aliases'].append({'path': target_root, 'target': seen[identity]})
                        continue
                    seen[identity] = target_root
                for section in sections:
                    for item in child.get(section, []):
                        item = dict(item)
                        storage_path = item['path']
                        item['path'] = mapped(storage_path)
                        if section in ('groups', 'datasets', 'named_types'):
                            item.update(storage_image=str(capture['image']), storage_source=str(capture['source']),
                                        storage_path=storage_path, storage_digest=capture['digest'])
                        if section == 'datasets':
                            item['aliases'] = [mapped(path) for path in item['aliases']]
                            for dimension in item['dimensions']:
                                dimension['scales'] = [mapped(path) for path in dimension['scales']]
                        elif section == 'group_aliases':
                            item['target'] = mapped(item['target'])
                        elif section == 'soft_links' and item['target'].startswith(source_root.rstrip('/') + '/'):
                            item['target'] = mapped(item['target'])
                        inventory.setdefault(section, []).append(item)
                for nested in child.get('external_links', []):
                    pending.append({**nested, 'path': mapped(nested['path'])})
                inventory['issues'].extend({**item, 'path': mapped(item['path'])} for item in child['issues'])
                inventory['skipped'].extend({**item, 'path': mapped(item['path'])} for item in child['skipped'])
                inventory['complete'] = inventory['complete'] and child['complete']
                if sum(len(inventory.get(section, [])) for section in sections) > self.budget.max_objects:
                    raise RecoveryError('related object graph exceeds the configured object budget')
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                inventory['skipped'].append({'path': link['path'], 'reason': str(exc)[:300]})
        # Multiple external paths to one dataset retain their original alias relationship.
        primary, datasets = {}, []
        for entry in inventory['datasets']:
            identity = (entry.get('storage_digest', main_digest), entry.get('address', entry['path']))
            if identity in primary:
                primary[identity]['aliases'].extend([entry['path'], *entry['aliases']])
            else:
                primary[identity] = entry
                datasets.append(entry)
        inventory['datasets'] = datasets
        return inventory

    def activate(self, image):
        from .source_session import share_image
        capture = next(value for value in self.captures.values() if str(value['image']) == str(image))
        return share_image(capture['image'], capture['digest'], capture['size'], capture['copied'], self.budget)

    def verify(self):
        for capture in self.captures.values():
            _verify_source(capture['source'], capture['identity'], capture['digest'])
