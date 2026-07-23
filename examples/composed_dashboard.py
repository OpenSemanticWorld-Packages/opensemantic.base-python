"""Example: composing several dashboards into one app with one aggregate config.

Shows the host-side composition pattern:

- Each view owns its own config (``DataToolViewConfig``) and is built
  ``embeddable=True`` so the host controls the layout.
- The host defines an aggregate ``AppConfig`` (a plain Pydantic model) with one
  slot per view plus its own global fields, and keeps each slot in sync via
  ``view.on_config_change``.
- The *parent* config is URL-synced once with the generic ``UrlConfig`` tooling,
  so the whole multi-view app state round-trips through a single URL param.

The per-view ``url_sync`` flag stays off here - the host owns persistence.

Run with:  panel serve examples/composed_dashboard.py
"""

import asyncio
import datetime as dt

import nest_asyncio
import panel as pn
from panelini import Panelini
from pydantic import BaseModel

from opensemantic import compute_scoped_uuid
from opensemantic.base._demo_data import seed_channel_series
from opensemantic.base.v1 import (
    Database,
    DataChannel,
    DataTool,
    DataToolController,
)
from opensemantic.base.view import (
    DataToolView,
    DataToolViewConfig,
    UrlConfig,
    UrlConfigMode,
)
from opensemantic.characteristics.quantitative.v1 import (
    Temperature,
    TemperatureUnit,
)
from opensemantic.core.v1 import Label

pn.extension()
nest_asyncio.apply()


def _controller(name: str, seed_base: float):
    from uuid import NAMESPACE_URL, uuid5

    puid = uuid5(NAMESPACE_URL, name)
    tool = DataTool(
        uuid=puid,
        name=name,
        label=[Label(text=name)],
        data_channels=[
            DataChannel(
                uuid=str(compute_scoped_uuid(puid, "temp")),
                osw_id="placeholder",
                name="temperature",
                label=[Label(text="Temperature")],
                characteristic=Temperature.get_cls_iri(),
            ),
        ],
        storage_locations=[Database(name=f"{name}_db", label=[Label(text="DB")])],
    )
    ctrl = DataToolController(tool, auto_archive=True)
    # Seed the last hour so the data falls in the view's default now-1h window.
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=59)
    asyncio.run(
        seed_channel_series(
            ctrl,
            n_points=60,
            base_ts=base,
            seconds_step=60,
            value_fn=lambda ch, i: Temperature(
                value=seed_base + i * 0.1, unit=TemperatureUnit.kelvin
            ),
        )
    )
    return ctrl


# One aggregate config: a slot per view plus global app state (the active tab).
class AppConfig(BaseModel):
    title: str = "Composed dashboards"
    active_tab: int = 0
    reactor: DataToolViewConfig = DataToolViewConfig()
    furnace: DataToolViewConfig = DataToolViewConfig()


app_config = AppConfig()

reactor = DataToolView(
    controllers=[_controller("Reactor", 300.0)],
    config=app_config.reactor,
    embeddable=True,
    title="Reactor",
)
furnace = DataToolView(
    controllers=[_controller("Furnace", 500.0)],
    config=app_config.furnace,
    embeddable=True,
    title="Furnace",
)

# URL-sync only the parent; each view updates its slot on change.
url = UrlConfig(AppConfig, param_name="app")


def _write_url():
    # PLAIN_KEYS: readable params. Fine here because each view's config is small
    # (the tree stores only selected keys), so the aggregate URL stays compact.
    url.set_config(app_config, UrlConfigMode.PLAIN_KEYS)


def _bind(view, field):
    def _update(cfg):
        setattr(app_config, field, cfg)
        _write_url()

    view.on_config_change(_update)


_bind(reactor, "reactor")
_bind(furnace, "furnace")


# One Panelini shell (wallpaper, styled sidebar, tree CSS, content scroll): the
# main area is a tab per view (its plot + log cards); the sidebar shows the
# active view's cards (channel tree, plot controls), swapped on tab change.
_views = [("Reactor", reactor), ("Furnace", furnace)]

main_tabs = pn.Tabs(
    *[
        (name, pn.Column(*v.main_cards, sizing_mode="stretch_width"))
        for name, v in _views
    ],
    sizing_mode="stretch_width",
)

app = Panelini(title=app_config.title, sidebar_enabled=True, sidebars_max_width=400)
app.main_set([main_tabs])


def _sync_sidebar(idx):
    app.sidebar_set(list(_views[idx][1].sidebar_cards))


def _on_tab(event):
    _sync_sidebar(event.new)
    app_config.active_tab = event.new
    _write_url()


main_tabs.param.watch(_on_tab, "active")
_sync_sidebar(0)


def _load_from_url():
    if url.has_config():
        loaded = url.get_config()
        reactor.set_config(loaded.reactor)
        furnace.set_config(loaded.furnace)
        app_config.active_tab = loaded.active_tab
        main_tabs.active = loaded.active_tab


pn.state.onload(_load_from_url)

app.servable()
