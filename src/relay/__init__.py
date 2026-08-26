"""Token consumption relay for Claude Code / Codex / OpenClaw."""

__version__ = "0.1.0"

# v0.117+ 把对插件作者/横向模块开发者**永久稳定**的 API 集中 re-export。
# 详见 docs/architecture/STABILITY.md。插件作者可以：
#     from relay._api_stable import PluginContext, load_plugins, register_wire_converter, ...
# 该 import 路径在 v0.130 之前不会变。
from . import _api_stable  # noqa: F401