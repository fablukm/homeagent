from homeagent.actions.base import (
    Action,
    ActionContext,
    ActionResult,
    ActionStatus,
    register_action,
)
from homeagent.sinks.base import Topic


@register_action("telegram.log")
class TelegramLog(Action):
    type = "telegram.log"

    async def run(self, ctx: ActionContext) -> ActionResult:
        text = ctx.render(str(getattr(ctx.spec, "text", "")))
        await ctx.sink.emit(Topic.LOG, text)
        return ActionResult(ActionStatus.TAKEN, detail=text)
