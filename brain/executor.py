"""执行器：按顺序把计划里的工具逐个交给 body，任何一步失败就中止后续步骤。"""
import asyncio
import logging

from common.tools import describe

log = logging.getLogger("executor")


class Executor:
    def __init__(self, body, report=None, set_current=None):
        self.body = body
        self.task = None
        # 可选：演示大屏的回调 report(kind, text) / set_current(text)
        self.report = report or (lambda kind, text: None)
        self.set_current = set_current or (lambda text: None)

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    async def start(self, steps):
        """开始执行新计划；如果旧计划还在跑，先打断（新指令优先）。"""
        await self.stop()
        self.task = asyncio.create_task(self._run(steps))

    async def wait(self):
        # 用 asyncio.wait 而不是 gather：外层被取消时不会连带取消 self.task，
        # 这样随后的 stop() 还能看到它在运行，并通知 body 停下。
        if self.task:
            await asyncio.wait([self.task])

    async def stop(self):
        if not self.running:
            return
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.body.cancel()

    async def _run(self, steps):
        n = len(steps)
        for i, (name, args) in enumerate(steps, 1):
            desc = describe(name, args)
            print(f"  ▶ [{i}/{n}] {desc}", flush=True)
            self.report("say" if name == "speak" else "step", f"[{i}/{n}] {desc}")
            self.set_current(f"[{i}/{n}] {desc}")
            ok, msg = await self.body.call(name, args)
            print(f"    {'✓' if ok else '✗'} {msg}", flush=True)
            self.report("result" if ok else "error", msg)
            if not ok:
                if i < n:
                    print(f"  ⚠ 第 {i} 步失败，后面 {n - i} 步不执行了", flush=True)
                    self.report("error", f"第 {i} 步失败，后面 {n - i} 步不执行了")
                self.set_current("")
                return
        print("  ✔ 全部完成", flush=True)
        self.report("result", "全部完成")
        self.set_current("")
