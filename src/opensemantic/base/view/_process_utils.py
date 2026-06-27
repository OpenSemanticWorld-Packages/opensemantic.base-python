"""Pure logic for the process/object-centered dashboard.

Builds, from a list of objects (Item instances), the processes they were inputs
to, the DataTool controllers attached to those processes, and a virtual
"aggregated channel" structure grouped by process type. No UI dependencies
(no Panel/Bokeh imports).

All entity relations are resolved by IRI via ``get_iri`` / ``get_iri_ref`` so the
logic works offline (no backend resolver required): an object qualifies for a
process when its IRI appears in the process's ``input`` references; a process's
``tool`` references are matched against the provided controllers by IRI.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from opensemantic.base.view._channel_utils import (
    get_characteristic_iri,
    get_display_label,
    get_display_label_cls,
    resolve_characteristic_label,
    resolve_value_type,
)

_logger = logging.getLogger(__name__)


# -- IRI / identity helpers (offline-safe) --


def entity_iri(obj: Any) -> Optional[str]:
    """Return an entity's IRI string, or None."""
    getter = getattr(obj, "get_iri", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def iri_refs(obj: Any, field: str) -> List[str]:
    """Return the IRI reference string(s) of a (range) field without resolving.

    Tries ``get_iri_ref`` then ``__iris__`` then a resolved-value fallback.
    """
    getter = getattr(obj, "get_iri_ref", None)
    refs = None
    if getter is not None:
        try:
            refs = getter(field)
        except Exception:
            refs = None
    if refs is None:
        iris = getattr(obj, "__iris__", {}) or {}
        refs = iris.get(field)
    if refs is None:
        # Fallback: field may hold inline objects/strings
        try:
            val = getattr(obj, field, None)
        except Exception:
            val = None
        if val is None:
            return []
        if not isinstance(val, list):
            val = [val]
        out = []
        for v in val:
            if isinstance(v, str):
                out.append(v)
            else:
                vi = entity_iri(v)
                if vi:
                    out.append(vi)
        return out
    if isinstance(refs, str):
        return [refs]
    return list(refs)


def type_key(obj: Any) -> Tuple[str, ...]:
    """Return the entity's ``type`` as a hashable tuple of category IRIs."""
    t = getattr(obj, "type", None) or []
    if isinstance(t, str):
        t = [t]
    return tuple(t)


def type_label(type_key_: Tuple[str, ...], lang: str = "en") -> str:
    """Human label for a type tuple: resolve the first IRI to a class label."""
    if not type_key_:
        return "?"
    iri = type_key_[0]
    try:
        from oold.model import _types

        cls = _types.get(iri)
        if cls is None:
            from oold.model.v1 import _types as _v1_types

            cls = _v1_types.get(iri)
        if cls is not None:
            lbl = get_display_label_cls(cls, lang)
            if lbl:
                return lbl
    except ImportError:
        pass
    return iri.split(":")[-1] if ":" in iri else iri


def _controllers_by_iri(controllers: List[Any]) -> Dict[str, Any]:
    """Map controller IRIs (and ``Item:`` + osw_id) to controllers."""
    out: Dict[str, Any] = {}
    for ctrl in controllers:
        iri = entity_iri(ctrl)
        if iri:
            out[iri] = ctrl
        osw_getter = getattr(ctrl, "get_osw_id", None)
        if osw_getter is not None:
            try:
                out["Item:" + ctrl.get_osw_id()] = ctrl
            except Exception:
                pass
    return out


# -- Concrete tree: object -> processes -> controllers --


def build_concrete_tree(
    objects: List[Any],
    processes: List[Any],
    controllers: List[Any],
    lang: str = "en",
) -> Dict[str, Dict[str, Any]]:
    """Build object -> qualifying-processes -> DataTool controllers.

    A process qualifies for an object when the object's IRI is one of the
    process ``input`` references, the process has both ``start_date_time`` and
    ``end_date_time``, and at least one of its ``tool`` references resolves to a
    provided DataTool controller.

    Returns ``{object_iri: {object, iri, label, processes: [{process, iri,
    label, controllers}]}}``.
    """
    by_iri = _controllers_by_iri(controllers)
    tree: Dict[str, Dict[str, Any]] = {}

    for obj in objects:
        obj_iri = entity_iri(obj)
        if obj_iri is None:
            _logger.warning("Object has no IRI, skipping: %r", obj)
            continue
        entry: Dict[str, Any] = {
            "object": obj,
            "iri": obj_iri,
            "label": get_display_label(obj, lang),
            "processes": [],
        }

        for proc in processes:
            if obj_iri not in iri_refs(proc, "input"):
                continue
            proc_id = entity_iri(proc) or get_display_label(proc, lang)
            start = getattr(proc, "start_date_time", None)
            end = getattr(proc, "end_date_time", None)
            if start is None or end is None:
                _logger.warning("Process %s skipped: missing start/end time", proc_id)
                continue

            proc_ctrls = []
            for tiri in iri_refs(proc, "tool"):
                ctrl = by_iri.get(tiri)
                if ctrl is None:
                    _logger.info(
                        "Process %s: tool %s is not a DataTool controller, " "skipping",
                        proc_id,
                        tiri,
                    )
                    continue
                proc_ctrls.append(ctrl)

            if not proc_ctrls:
                _logger.warning("Process %s skipped: no DataTool attached", proc_id)
                continue

            entry["processes"].append(
                {
                    "process": proc,
                    "iri": entity_iri(proc),
                    "label": get_display_label(proc, lang),
                    "controllers": proc_ctrls,
                }
            )

        tree[obj_iri] = entry

    return tree


# -- Virtual structure: aggregated channels grouped by process type --


Signature = Tuple[Tuple[str, ...], Optional[str], Optional[str]]


def _sig_str(sig: Signature) -> str:
    dt_key, name, char = sig
    return "+".join(dt_key) + "::" + str(name) + "::" + str(char)


def _agg_key_merged(process_type: Tuple[str, ...], sig: Signature) -> str:
    return "+".join(process_type) + "##" + _sig_str(sig) + "##agg"


def _agg_key_instance(
    process_type: Tuple[str, ...], sig: Signature, datatool_iri: str
) -> str:
    return "+".join(process_type) + "##" + _sig_str(sig) + "##inst::" + datatool_iri


def process_type_node_key(process_type: Tuple[str, ...]) -> str:
    """Stable key for a process-type root node."""
    return "ptype::" + "+".join(process_type)


def _plural(n: int) -> str:
    return "channel" if n == 1 else "channels"


def derive_aggregated_channels(
    concrete_tree: Dict[str, Dict[str, Any]],
    lang: str = "en",
) -> Dict[Tuple[str, ...], Dict[str, Any]]:
    """Group qualifying processes by type and derive treeview channel entries.

    Aggregation is only a treeview convenience (tick once instead of many).
    Channels share a *signature* ``(datatool_type, channel_name,
    characteristic_iri)``. Within a process type, two datatool instances of the
    same signature are merged only when they are **never co-present in the same
    process run** (treated as drop-in replacements). Datatool instances that do
    co-occur in some run are kept as **separate per-instance entries**, since
    they are distinct measurement points.

    So per signature within a process type:
    - co-present instances -> one entry each, labelled by datatool instance;
    - the remaining (never co-present) instances -> one merged entry labelled
      ``<type>/<channel> [n channels]`` whose tooltip lists the actual channels.

    Composite/unknown channels are skipped (info-logged).

    Returns ``{process_type: {process_type, label, channels: {agg_key: agg}}}``.
    Each ``agg`` carries ``datatool_iris`` (the datatool instances it
    represents), ``aggregated_channels`` (display strings), ``n_channels`` and an
    ``aggregated`` flag.
    """
    # Pass 1: gather, per process type, the runs (sig -> set of datatool IRIs)
    # and per (pt, sig) the instance metadata.
    pt_runs: Dict[Tuple[str, ...], List[Dict[Signature, set]]] = {}
    pt_sig_inst: Dict[Tuple[str, ...], Dict[Signature, Dict[str, Dict[str, Any]]]] = {}
    pt_label: Dict[Tuple[str, ...], str] = {}

    for obj_entry in concrete_tree.values():
        for pe in obj_entry["processes"]:
            proc = pe["process"]
            pt = type_key(proc)
            pt_label.setdefault(pt, type_label(pt, lang))
            run_sig: Dict[Signature, set] = {}
            for ctrl in pe["controllers"]:
                dt_key = type_key(ctrl)
                ctrl_iri = entity_iri(ctrl)
                if ctrl_iri is None:
                    continue
                for ch in ctrl.get_all_channels():
                    vtype = resolve_value_type(ch)
                    if vtype in ("composite", "unknown"):
                        _logger.info(
                            "Channel %s (%s) excluded from aggregation",
                            getattr(ch, "name", "?"),
                            vtype,
                        )
                        continue
                    name = getattr(ch, "name", None)
                    char_iri = get_characteristic_iri(ch)
                    sig: Signature = (dt_key, name, char_iri)
                    run_sig.setdefault(sig, set()).add(ctrl_iri)
                    inst = pt_sig_inst.setdefault(pt, {}).setdefault(sig, {})
                    if ctrl_iri not in inst:
                        inst[ctrl_iri] = {
                            "datatool_label": get_display_label(ctrl, lang),
                            "channel_name": name,
                            "value_type": vtype,
                            "characteristic_label": resolve_characteristic_label(
                                ch, lang
                            ),
                        }
            pt_runs.setdefault(pt, []).append(run_sig)

    # Pass 2: per process type + signature, split co-present vs free instances.
    groups: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for pt, sig_inst in pt_sig_inst.items():
        grp: Dict[str, Any] = {
            "process_type": pt,
            "label": pt_label[pt],
            "channels": {},
        }
        # Instances co-present with another same-signature instance in a run.
        copresent: Dict[Signature, set] = {}
        for run_sig in pt_runs[pt]:
            for sig, iris in run_sig.items():
                if len(iris) >= 2:
                    copresent.setdefault(sig, set()).update(iris)

        for sig, inst_map in sig_inst.items():
            dt_key, name, char_iri = sig
            co = copresent.get(sig, set())

            # Per-instance entries for co-present datatools.
            for iri, meta in inst_map.items():
                if iri not in co:
                    continue
                key = _agg_key_instance(pt, sig, iri)
                grp["channels"][key] = {
                    "key": key,
                    "process_type": pt,
                    "datatool_type": dt_key,
                    "channel_name": name,
                    "characteristic_iri": char_iri,
                    "characteristic_label": meta["characteristic_label"],
                    "value_type": meta["value_type"],
                    "aggregated": False,
                    "datatool_iris": [iri],
                    "n_channels": 1,
                    "aggregated_channels": [f"{meta['datatool_label']}/{name}"],
                    "label": f"{meta['datatool_label']}/{name}",
                }

            # One merged entry for the remaining (never co-present) instances.
            free = [iri for iri in inst_map if iri not in co]
            if free:
                n = len(free)
                chans = [f"{inst_map[i]['datatool_label']}/{name}" for i in free]
                meta0 = inst_map[free[0]]
                type_lbl = type_label(dt_key, lang)
                key = _agg_key_merged(pt, sig)
                grp["channels"][key] = {
                    "key": key,
                    "process_type": pt,
                    "datatool_type": dt_key,
                    "channel_name": name,
                    "characteristic_iri": char_iri,
                    "characteristic_label": meta0["characteristic_label"],
                    "value_type": meta0["value_type"],
                    "aggregated": True,
                    "datatool_iris": list(free),
                    "n_channels": n,
                    "aggregated_channels": chans,
                    "label": f"{type_lbl}/{name} [{n} {_plural(n)}]",
                }

        groups[pt] = grp

    return groups


def resolve_aggregated_channel(
    object_entry: Dict[str, Any],
    agg: Dict[str, Any],
) -> List[Tuple[Any, Any, Any]]:
    """Resolve a treeview entry to concrete (controller, channel, process).

    Each entry represents a specific set of datatool instances
    (``agg['datatool_iris']``): one instance for a per-instance entry, or the
    pooled drop-in replacements for a merged entry. This returns one tuple per
    matching physical channel across the object's processes - so a selection
    fans out over the runs (and, for merged entries, the pooled datatools) the
    object actually has data on. The view labels each line distinctly by
    object / process / datatool / channel.
    """
    allowed = set(agg.get("datatool_iris") or [])
    out: List[Tuple[Any, Any, Any]] = []
    for pe in object_entry["processes"]:
        proc = pe["process"]
        if type_key(proc) != agg["process_type"]:
            continue
        for ctrl in pe["controllers"]:
            if entity_iri(ctrl) not in allowed:
                continue
            for ch in ctrl.get_all_channels():
                if getattr(ch, "name", None) != agg["channel_name"]:
                    continue
                if get_characteristic_iri(ch) != agg["characteristic_iri"]:
                    continue
                out.append((ctrl, ch, proc))
    return out


# -- Wunderbaum tree-source builders --


def build_object_tree_source(
    concrete_tree: Dict[str, Dict[str, Any]],
    lang: str = "en",
) -> List[Dict[str, Any]]:
    """Flat checkable list of objects; key = object IRI."""
    source = []
    for obj_iri, entry in concrete_tree.items():
        n_proc = len(entry["processes"])
        source.append(
            {
                "title": entry["label"] or obj_iri,
                "key": obj_iri,
                "checkbox": True,
                "selected": False,
                "processes": str(n_proc),
                "tooltip": (f"{entry['label']}\nIRI: {obj_iri}\nProcesses: {n_proc}"),
            }
        )
    return source


def build_process_tree_source(
    aggregated: Dict[Tuple[str, ...], Dict[str, Any]],
    lang: str = "en",
) -> List[Dict[str, Any]]:
    """Process-type roots with aggregated channels as checkable children."""
    source = []
    for pt, grp in aggregated.items():
        children = []
        for agg in grp["channels"].values():
            chans = agg.get("aggregated_channels", [])
            chan_lines = "\n".join(f"  - {c}" for c in chans)
            children.append(
                {
                    "title": agg["label"],
                    "key": agg["key"],
                    "checkbox": True,
                    "selected": False,
                    "characteristic": agg["characteristic_label"],
                    "tooltip": (
                        f"{agg['label']}\n"
                        f"Characteristic: {agg['characteristic_label']}\n"
                        f"Channel: {agg['channel_name']}\n"
                        f"Aggregated channels ({agg.get('n_channels', len(chans))}):\n"
                        f"{chan_lines}"
                    ),
                }
            )
        source.append(
            {
                "title": grp["label"],
                "key": process_type_node_key(pt),
                "expanded": True,
                "checkbox": True,
                "children": children,
            }
        )
    return source


def get_selected_keys(source: List[Dict[str, Any]]) -> List[str]:
    """Collect keys of checked leaf nodes (and checked childless roots)."""
    keys = []
    for node in source:
        children = node.get("children")
        if children:
            for child in children:
                if child.get("selected"):
                    keys.append(child.get("key"))
        elif node.get("selected"):
            keys.append(node.get("key"))
    return keys
