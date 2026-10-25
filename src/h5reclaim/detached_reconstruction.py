"""Reconstruct checked detached group links under address-derived paths.

The checked group header establishes its internal hard links, including links
shared by several groups. It cannot establish the group's missing parent name.
Each root therefore gets a synthetic path, and repeated object addresses are
represented by hard links to one exported object. Unreadable targets and group
cycles are recorded as omitted edges, never guessed measurement values.
"""

from __future__ import annotations

from collections import Counter


def detached_hierarchies(discovery: dict, known_groups: set[int],
                         known_datasets: set[int], *, max_objects: int,
                         max_links: int, blocked_paths: set[str] | None = None) -> dict:
    """Plan independently checked detached objects and their safe hard links.

    Each dataset has one canonical export path. Other checked incoming links
    become aliases to that same object, including links from separate roots.
    A cyclic group edge cannot be materialized by HDF5 and is retained only as
    evidence. Reachable valid branches of an incomplete group remain usable.
    """
    blocked = set(blocked_paths or ())
    datasets = {item["address"] for item in discovery["datasets"]}
    candidates = {item["address"]: item for item in discovery.get("groups", ())
                  if item["address"] not in known_groups
                  and item["address"] != discovery["root_object_address"]}
    incoming: Counter[int] = Counter()
    for item in discovery.get("groups", ()):
        for link in item["links"]:
            if link["address"] in candidates:
                incoming[link["address"]] += 1

    roots = sorted(address for address in candidates if not incoming[address])
    group_paths: dict[int, str] = {}
    dataset_paths: dict[int, str] = {}
    dataset_aliases: dict[int, list[str]] = {}
    group_aliases: list[dict] = []
    omitted_links: list[dict] = []
    accepted_paths = set(blocked)
    link_count = 0

    for root in roots:
        root_path = f"/recovered/group_{root:x}"
        if root_path in accepted_paths:
            omitted_links.append({"group_address": root, "reason": "synthetic root path is occupied"})
            continue
        pending = [(root, root_path)]
        component_groups: dict[int, str] = {}
        component_datasets: dict[int, str] = {}
        component_dataset_aliases: dict[int, list[str]] = {}
        component_group_aliases: list[dict] = []
        component_paths: set[str] = set()
        component_omissions: list[dict] = []
        while pending:
            address, path = pending.pop()
            if address in group_paths or address in component_groups:
                target = (component_groups | group_paths)[address]
                # HDF5 cannot represent a group hard link back to an ancestor.
                if path.startswith(target + "/"):
                    component_omissions.append({"group_address": address, "path": path,
                                                "reason": "cyclic group hard link"})
                else:
                    component_group_aliases.append({"path": path, "target": target})
                    component_paths.add(path)
                continue
            if len(group_paths) + len(component_groups) + len(dataset_paths) + len(component_datasets) >= max_objects:
                component_omissions.append({"group_address": address, "path": path,
                                            "reason": "object budget exceeded"})
                continue
            component_groups[address] = path
            component_paths.add(path)
            for link in candidates[address]["links"]:
                if link_count >= max_links:
                    component_omissions.append({"group_address": address,
                                                "reason": "link budget exceeded; remaining links uninspected"})
                    break
                link_count += 1
                child, name = link["address"], link["name"]
                reason = None
                if (not isinstance(name, str) or not name or "/" in name
                      or "\x00" in name or name in (".", "..")):
                    reason = "invalid local link name"
                elif len((path + "/" + name).encode("utf-8")) > 16384:
                    reason = "synthetic path exceeds 16384 bytes"
                if reason:
                    component_omissions.append({"group_address": address, "name": str(name)[:200],
                                                "target_address": child, "reason": reason})
                    continue
                child_path = path + "/" + name
                if child_path in accepted_paths or child_path in component_paths:
                    component_omissions.append({"group_address": address, "name": name,
                                                "target_address": child, "reason": "output path is occupied"})
                elif child in candidates:
                    component_paths.add(child_path)
                    pending.append((child, child_path))
                elif child in datasets or child in known_datasets:
                    component_paths.add(child_path)
                    if child not in dataset_paths and child not in component_datasets and child not in known_datasets:
                        if len(group_paths) + len(component_groups) + len(dataset_paths) + len(component_datasets) >= max_objects:
                            component_omissions.append({"group_address": address, "name": name,
                                                        "target_address": child, "reason": "object budget exceeded"})
                            component_paths.remove(child_path)
                            continue
                        component_datasets[child] = child_path
                    else:
                        component_dataset_aliases.setdefault(child, []).append(child_path)
                else:
                    component_omissions.append({"group_address": address, "name": name,
                                                "target_address": child,
                                                "reason": "target is not an independently checked dataset or group"})

        omitted_links.extend(component_omissions)
        if not (component_datasets or component_dataset_aliases or component_group_aliases):
            continue
        group_paths.update(component_groups)
        dataset_paths.update(component_datasets)
        for address, aliases in component_dataset_aliases.items():
            dataset_aliases.setdefault(address, []).extend(aliases)
        group_aliases.extend(component_group_aliases)
        accepted_paths.update(component_paths)

    groups = [{"path": path, "address": address, "attributes": [],
               "attributes_omitted": ["detached group attributes were not exported"],
               "detached": True}
              for address, path in sorted(group_paths.items(),
                                          key=lambda item: (item[1].count("/"), item[1]))]
    return {"groups": groups, "dataset_paths": dataset_paths,
            "dataset_aliases": dataset_aliases, "group_aliases": group_aliases,
            "omitted_links": omitted_links,
            "scope": "synthetic roots with checked internal hard links, including shared objects; "
                     "missing parent paths and omitted edges are not inferred"}
