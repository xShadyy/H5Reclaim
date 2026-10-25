"""Assign synthetic paths to independently checked detached group trees.

Only a group's *internal* link names survive in its checked object header.
The lost parent path is unknowable, so each root receives an address-derived
name. Shared, cyclic, externally linked or incomplete trees are left as
individual object-address candidates instead of assigning guessed paths.
"""

from __future__ import annotations

from collections import Counter


def detached_hierarchies(discovery: dict, known_groups: set[int],
                         known_datasets: set[int], *, max_objects: int,
                         max_links: int, blocked_paths: set[str] | None = None) -> dict:
    """Return paths only when every link in a candidate tree is accounted for."""
    datasets = {item["address"] for item in discovery["datasets"]}
    candidates = {item["address"]: item for item in discovery.get("groups", ())
                  if item["address"] not in known_groups
                  and item["address"] != discovery["root_object_address"]}
    owners: Counter[int] = Counter()
    for item in discovery.get("groups", ()):
        for link in item["links"]:
            owners[link["address"]] += 1
    roots = sorted(address for address in candidates if not owners[address])
    plans: list[tuple[set[int], dict[int, str], dict[int, str]]] = []
    for root in roots:
        pending = [(root, f"/recovered/group_{root:x}")]
        visited: set[int] = set()
        group_paths: dict[int, str] = {}
        dataset_paths: dict[int, str] = {}
        links_seen = 0
        complete = True
        while pending:
            address, path = pending.pop()
            if address in visited or address not in candidates:
                complete = False
                break
            visited.add(address)
            group_paths[address] = path
            if len(visited) + len(dataset_paths) > max_objects:
                complete = False
                break
            for link in candidates[address]["links"]:
                links_seen += 1
                child, name = link["address"], link["name"]
                if (links_seen > max_links or owners[child] != 1 or not isinstance(name, str)
                        or not name or "/" in name or name in (".", "..")):
                    complete = False
                    break
                child_path = path + "/" + name
                if child in candidates:
                    pending.append((child, child_path))
                elif child in datasets and child not in known_datasets and child not in dataset_paths:
                    dataset_paths[child] = child_path
                else:
                    complete = False
                    break
            if not complete:
                break
        if complete and dataset_paths:
            plans.append((visited | set(dataset_paths), group_paths, dataset_paths))

    # A valid-looking stale group could refer to the same object as another
    # candidate tree. Refuse both claims rather than select a historic name.
    shared: Counter[int] = Counter(address for footprint, _, _ in plans for address in footprint)
    groups: list[dict] = []
    paths: dict[int, str] = {}
    for footprint, group_paths, dataset_paths in plans:
        if any(shared[address] != 1 for address in footprint):
            continue
        if any(path in (blocked_paths or ()) for path in (*group_paths.values(), *dataset_paths.values())):
            continue
        groups.extend({"path": path, "address": address, "attributes": [],
                       "attributes_omitted": ["detached group attributes were not exported"],
                       "detached": True}
                      for address, path in sorted(group_paths.items(), key=lambda item: item[1].count("/")))
        paths.update(dataset_paths)
    return {"groups": groups, "dataset_paths": paths,
            "scope": "synthetic detached roots with checked group-internal links; no lost parent path inferred"}
