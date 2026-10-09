"""build_app wiring and policy hot reload."""

from __future__ import annotations

import os

from mcp_proton.config import config_dir, load_policy, save_policy
from mcp_proton.policy.model import Preset
from mcp_proton.services.factory import build_app, make_policy_loader


def test_policy_loader_caches_and_reloads(monkeypatch):
    save_policy(load_policy())
    calls = []
    import mcp_proton.services.factory as f

    real = f.load_policy
    monkeypatch.setattr(f, "load_policy", lambda d=None: calls.append(1) or real(d))
    loader = make_policy_loader()
    first = loader()
    assert loader() is first and len(calls) == 1
    pol = load_policy()
    pol.preset = Preset.READER
    save_policy(pol)
    p = config_dir() / "policy.toml"
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert loader().preset is Preset.READER and len(calls) == 2


def test_build_app_reflects_policy_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_PROTON_CONFIG_DIR", str(tmp_path / "c"))
    cfg_dir = tmp_path / "c"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(f'data_dir = "{tmp_path / "d"}"\n')
    app = build_app(cfg_dir, store_factory=lambda n: None, transport_factory=lambda n: None)
    assert app.policy.preset is None
    pol = load_policy(cfg_dir)
    pol.preset = Preset.ASSISTANT
    save_policy(pol, cfg_dir)
    assert app.policy.preset is Preset.ASSISTANT
    assert (tmp_path / "d" / "mcp-proton.sqlite3").exists()
    app.close()
