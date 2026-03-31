import json
import sys
from pathlib import Path

_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from setup_realtime import setup_settings  # noqa: E402
from utils.config import (  # noqa: E402
    SDKConfig,
    resolve_openclaw_gateway_port,
    _DEFAULT_GATEWAY_PORT,
)


def test_resolve_gateway_port_default(tmp_path, monkeypatch):
    """When no env or config is present, fall back to default port."""
    monkeypatch.delenv("OPENCLAW_GATEWAY_PORT", raising=False)
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "openclaw.json"))

    assert resolve_openclaw_gateway_port() == _DEFAULT_GATEWAY_PORT


def test_resolve_gateway_port_custom_from_config(tmp_path, monkeypatch):
    """When openclaw.json has gateway.port, use that value."""
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps({"gateway": {"port": 25307}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENCLAW_GATEWAY_PORT", raising=False)
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    assert resolve_openclaw_gateway_port() == 25307


def test_setup_settings_updates_webhook_urls_with_detected_port(tmp_path, monkeypatch):
    """setup_settings always updates webhook URLs to the detected gateway port."""
    # Isolate data dir into tmp_path
    data_dir = tmp_path / "data" / "awiki-agent-id-message"
    monkeypatch.setenv("AWIKI_DATA_DIR", str(data_dir))

    # Prepare fake openclaw.json with custom port
    openclaw_config = tmp_path / "openclaw.json"
    openclaw_config.write_text(
        json.dumps({"gateway": {"port": 25307}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(openclaw_config))

    # Seed an existing settings.json with old (wrong) port
    config = SDKConfig()
    settings_path = config.data_dir / "config" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "listener": {
            "mode": "smart",
            "agent_webhook_url": "http://127.0.0.1:18789/hooks/agent",
            "wake_webhook_url": "http://127.0.0.1:18789/hooks/wake",
            "webhook_token": "old-token",
        }
    }
    settings_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    # Run setup_settings, which should overwrite webhook URLs with detected port
    config = SDKConfig()
    result = setup_settings(
        config=config,
        token="new-token",
        receive_mode="websocket",
        local_daemon_token="local-token",
    )

    assert result["status"] == "ok"

    # Reload settings and verify webhook URLs use the new port
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    listener = saved["listener"]
    assert listener["agent_webhook_url"].endswith(":25307/hooks/agent")
    assert listener["wake_webhook_url"].endswith(":25307/hooks/wake")
