"""Converter between QMK-like and ZMK Studio DTS physical layout formats, with visualizer."""

import io
import json
from textwrap import indent

import streamlit as st

from keymap_drawer.draw import KeymapDrawer
from keymap_drawer.config import DrawConfig
from keymap_drawer.physical_layout import layout_factory, QmkLayout
from keymap_drawer.parse.dts import DeviceTree

DTS_TEMPLATE = """\
#include <physical_layouts.dtsi>

/ {{
{pl_nodes}\
}};
"""
PL_TEMPLATE = """\
layout_{idx}: layout_{idx} {{
    compatible = "zmk,physical-layout";
    display-name = "{name}";

    kscan = <&kscan_{idx}>;
    transform = <&matrix_transform_{idx}>;
{keys}\
}};
"""
KEYS_TEMPLATE = """
keys  //                     w   h    x    y     rot   rx   ry
    = {key_attrs_string}
    ;
"""
KEY_TEMPLATE = "<&key_physical_attrs {w:>3d} {h:>3d} {x:>4d} {y:>4d} {rot} {rx:>4d} {ry:>4d}>"
PHYSICAL_ATTR_PHANDLES = {"&key_physical_attrs"}


def _normalize_layout(qmk_spec: QmkLayout) -> QmkLayout:
    min_x, min_y = min(k.x for k in qmk_spec.layout), min(k.y for k in qmk_spec.layout)
    for key in qmk_spec.layout:
        key.x -= min_x
        key.y -= min_y
        if key.rx is not None:
            key.rx -= min_x
        if key.ry is not None:
            key.ry -= min_y
    return qmk_spec


@st.cache_data
def _get_initial_layout():
    with open("example.json", encoding="utf-8") as f:
        return f.read()


@st.cache_data(max_entries=10)
def dts_to_layouts(dts_str: str) -> dict[str, QmkLayout]:
    """Convert given DTS string containing physical layouts to internal QMK layout format."""
    dts = DeviceTree(dts_str, None, True)

    def parse_binding_params(bindings):
        params = {
            k: int(v.lstrip("(").rstrip(")")) / 100 for k, v in zip(("w", "h", "x", "y", "r", "rx", "ry"), bindings)
        }
        if params["r"] == 0:
            del params["rx"], params["ry"]
        return params

    bindings_to_position = {"key_physical_attrs": parse_binding_params}

    if nodes := dts.get_compatible_nodes("zmk,physical-layout"):
        defined_layouts = {node.get_string("display-name"): node.get_phandle_array("keys") for node in nodes}
    elif keys_array := dts.root.get_phandle_array("keys"):
        defined_layouts = {"Default": keys_array}
    else:
        raise ValueError('No `compatible = "zmk,physical-layout"` nodes nor a single `keys` property found')

    out_layouts = {}
    for display_name, position_bindings in defined_layouts.items():
        assert display_name is not None, "No `display_name` property found for a physical layout node"
        assert position_bindings is not None, f'No `keys` property found for layout "{display_name}"'
        keys = []
        for binding_arr in position_bindings:
            binding = binding_arr.split()
            assert binding[0].lstrip("&") in bindings_to_position, f"Unrecognized position binding {binding[0]}"
            keys.append(bindings_to_position[binding[0].lstrip("&")](binding[1:]))
        out_layouts[display_name] = _normalize_layout(QmkLayout(layout=keys))
    return out_layouts


def layout_to_svg(qmk_layout: QmkLayout) -> str:
    """Convert given internal QMK layout format to its SVG visualization."""
    physical_layout = qmk_layout.generate(60)
    with io.StringIO() as out:
        drawer = KeymapDrawer(
            config=DrawConfig(append_colon_to_layer_header=False, dark_mode="auto"),
            out=out,
            layers={"": list(range(len(physical_layout)))},
            layout=physical_layout,
        )
        drawer.print_board()
        return out.getvalue()


def layouts_to_json(layouts_map: dict[str, QmkLayout]) -> str:
    """Convert given internal QMK layout formats map to JSON representation."""
    out_layouts = {
        display_name: {"layout": qmk_layout.model_dump(exclude_defaults=True, exclude_unset=True)["layout"]}
        for display_name, qmk_layout in layouts_map.items()
    }
    return json.dumps({"layouts": out_layouts}, indent=2)


def layouts_to_dts(layouts_map: dict[str, QmkLayout]) -> str:
    """Convert given internal QMK layout formats map to DTS representation."""

    def rot_to_str(rot: float) -> str:
        rot = int(100 * rot)
        if rot >= 0:
            return f"{rot:>7d}"
        return f"{'(' + str(rot) + ')':>7}"

    pl_nodes = []
    for idx, (name, qmk_spec) in enumerate(layouts_map.items()):
        keys = KEYS_TEMPLATE.format(
            key_attrs_string="\n    , ".join(
                KEY_TEMPLATE.format(
                    w=int(100 * key.w),
                    h=int(100 * key.h),
                    x=int(100 * key.x),
                    y=int(100 * key.y),
                    rot=rot_to_str(key.r),
                    rx=int(100 * (key.rx or 0)),
                    ry=int(100 * (key.ry or 0)),
                )
                for key in qmk_spec.layout
            )
        )
        pl_nodes.append(PL_TEMPLATE.format(idx=idx, name=name, keys=indent(keys, "    ")))
    return DTS_TEMPLATE.format(pl_nodes=indent("\n".join(pl_nodes), "    "))


def qmk_json_to_layouts(qmk_info_str: str) -> dict[str, QmkLayout]:
    """Convert given QMK-style JSON string layouts format map to internal QMK layout formats map."""
    qmk_info = json.loads(qmk_info_str)

    if isinstance(qmk_info, list):
        return {"Default": QmkLayout(layout=qmk_info)}  # shortcut for list-only representation
    return {name: _normalize_layout(QmkLayout(layout=val["layout"])) for name, val in qmk_info["layouts"].items()}


def ortho_to_layouts(
    ortho_layout: dict | None, cols_thumbs_notation: str | None, split_gap: float = 1.0
) -> dict[str, QmkLayout]:
    """Given ortho specs (ortho layout description or cols+thumbs notation) convert it to the internal QMK layout format."""
    p_layout = layout_factory(
        DrawConfig(key_w=1, key_h=1, split_gap=split_gap),
        ortho_layout=ortho_layout,
        cols_thumbs_notation=cols_thumbs_notation,
    )
    return {
        "Default": QmkLayout(
            layout=[
                {"x": key.pos.x - key.width / 2, "y": key.pos.y - key.height / 2, "w": key.width, "h": key.height}
                for key in p_layout.keys
            ]
        )
    }


def _ortho_form() -> dict[str, QmkLayout] | None:
    out = None
    nonsplit, split, cols_thumbs = st.tabs(["Non-split", "Split", "Cols+Thumbs Notation"])
    with nonsplit:
        with st.form("ortho_nonsplit"):
            params = {
                "split": False,
                "rows": st.number_input("Number of rows", min_value=1, max_value=10),
                "columns": st.number_input("Number of columns", min_value=1, max_value=20),
                "thumbs": {"Default (1u)": 0, "MIT (1x1u)": "MIT", "2x2u": "2x2u"}[
                    st.selectbox("Thumbs type", options=("Default (1u)", "MIT (1x1u)", "2x2u"))  # type: ignore
                ],
                "drop_pinky": st.checkbox("Drop pinky"),
                "drop_inner": st.checkbox("Drop inner index"),
            }
            submitted = st.form_submit_button("Generate")
            if submitted:
                out = ortho_to_layouts(ortho_layout=params, cols_thumbs_notation=None)
    with split:
        with st.form("ortho_split"):
            params = {
                "split": True,
                "rows": st.number_input("Number of rows", min_value=1, max_value=10),
                "columns": st.number_input("Number of columns", min_value=1, max_value=10),
                "thumbs": st.number_input("Number of thumb keys", min_value=0, max_value=10),
                "drop_pinky": st.checkbox("Drop pinky"),
                "drop_inner": st.checkbox("Drop inner index"),
            }
            split_gap = st.number_input("Gap between split halves", value=1.0, min_value=0.0, max_value=10.0, step=0.5)
            submitted = st.form_submit_button("Generate")
            if submitted:
                out = ortho_to_layouts(ortho_layout=params, cols_thumbs_notation=None, split_gap=split_gap)
    with cols_thumbs:
        with st.form("ortho_cpt"):
            cpt_spec = st.text_input("Cols+Thumbs notation spec", placeholder="23333+2 3+333331")
            split_gap = st.number_input("Gap between split halves", value=1.0, min_value=0.0, max_value=10.0, step=0.5)
            submitted = st.form_submit_button("Generate")
            if submitted:
                out = ortho_to_layouts(ortho_layout=None, cols_thumbs_notation=cpt_spec, split_gap=split_gap)
    return out


def main() -> None:
    """Main body of the web app."""
    need_rerun = False
    if "layouts" not in st.session_state:
        st.session_state.layouts = None

    st.set_page_config(page_title="ZMK physical layout converter", page_icon=":keyboard:", layout="wide")
    st.html('<style>textarea[class^="st-"] { font-family: monospace; font-size: 12px; }</style>')
    st.header("ZMK physical layouts converter")
    st.caption("Tool to convert and visualize physical layout representations for ZMK Studio")

    with st.popover("Initialize from ortho params"):
        ortho_layout = _ortho_form()

    json_col, dts_col, svg_col = st.columns([0.25, 0.4, 0.35], vertical_alignment="top")

    update_from_json = False

    with json_col:
        st.subheader(
            "JSON format description",
            help="QMK-like physical layout spec description, similar to `qmk_info_json` option mentioned in the "
            "[docs](https://github.com/caksoylar/keymap-drawer/blob/main/KEYMAP_SPEC.md#qmk-infojson-specification).",
        )
        if new_val := st.session_state.get("json_field_update"):
            st.session_state.json_field = new_val
            st.session_state.json_field_update = None
            update_from_json = True
            print("3 set json from dts")
        elif ortho_layout:
            st.session_state.json_field = layouts_to_json(ortho_layout)
            update_from_json = True
            print("4 set initial json from ortho")
        elif st.session_state.layouts is None:
            print("1 set initial json value")
            st.session_state.json_field = _get_initial_layout()
            update_from_json = True

        st.text_area("JSON layout", key="json_field", height=800, label_visibility="collapsed")
        json_button = st.button("Update DTS using this ➡️")
        if update_from_json or json_button:
            print("2 updating rest from json")
            st.session_state.layouts = qmk_json_to_layouts(st.session_state.json_field)
            st.session_state.dts_field = layouts_to_dts(st.session_state.layouts)

    with dts_col:
        st.subheader(
            "ZMK DTS",
            help="Docs TBD on the format",
        )
        st.text_area("Devicetree", key="dts_field", height=800, label_visibility="collapsed")
        dts_button = st.button("⬅️Update JSON using this")
        if dts_button:
            print("5 updating rest from dts")
            st.session_state.layouts = dts_to_layouts(st.session_state.dts_field)
            st.session_state.json_field_update = layouts_to_json(st.session_state.layouts)
            need_rerun = True

    with svg_col:
        st.subheader("Visualization")
        if st.session_state.layouts is not None:
            svgs = {name: layout_to_svg(layout) for name, layout in st.session_state.layouts.items()}
            tabs = st.tabs(list(svgs))
            for i, svg in enumerate(svgs.values()):
                tabs[i].image(svg)

    if need_rerun:
        st.rerun()


if __name__ == "__main__":
    main()
