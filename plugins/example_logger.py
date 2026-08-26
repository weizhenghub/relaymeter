"""V0.0.1 示例插件 —— 演示插件平台的四个面：钩子 / 事件 / 请求头注入。

启用方式：本文件位于项目根 ``plugins/`` 目录（或 ``RELAY_PLUGINS_DIR``
指向的目录），启动中继自动加载。每次请求会打一行 INFO 日志 + 给上游
请求加一个头 —— 这就是"看到效果"的方式。看完可以随时删除本文件，
或改为注释掉 apply 里的注册块。

插件契约：模块必须导出 ``apply(ctx)``。ctx 能力见 relay/plugin.py。
"""

from __future__ import annotations


def apply(ctx) -> None:
    ctx.log.info("[example] 插件已加载 —— 插件平台 V0.0.1 生效")

    @ctx.register_hook("pre_dispatch")
    async def pre_dispatch(info):
        # 分发判定完成后：记录每次请求的去向（可改 info["model"]/["body"]）。
        ctx.log.info(
            "[example] pre_dispatch platform=%s upstream=%s model=%s passthrough=%s",
            info["platform"], info["cfg"].name, info["model"], info["passthrough"],
        )

    @ctx.register_hook("pre_upstream")
    def pre_upstream(info):
        # 发送前最后一改：给上游请求加一个头（info["headers"] 是 dict，
        # 可原地改或整体替换）。
        info["headers"]["x-relay-example"] = "plugin-v0.0.1"
        ctx.log.info("[example] pre_upstream url=%s", info["upstream_url"])

    @ctx.register_hook("post_response")
    async def post_response(info):
        # 请求完成后：usage 是 UsageAcc 对象（字段见 models.py）。
        u = info["usage"]
        ctx.log.info(
            "[example] post_response status=%s in=%s out=%s cache_read=%s error=%s",
            info["status"], u.input_tokens, u.output_tokens,
            u.cache_read_input_tokens, info["error"],
        )

    @ctx.on("request.done")
    def on_request_done(**payload):
        # 事件订阅：payload 与 post_response 的 info 同字段。
        ctx.log.info(
            "[example] 事件 request.done platform=%s model=%s",
            payload.get("platform"), payload.get("model"),
        )

    ctx.push_alert("example_logger 插件已加载（V0.0.1 演示）")