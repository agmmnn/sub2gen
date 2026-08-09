from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConversationTarget:
    project: str = "sub2gen"
    keep_conversation: bool = False
    keep_tab: bool = False

    def cli_args(self) -> tuple[str, ...]:
        args = ["--project", self.project]
        if self.keep_conversation:
            args.append("--keep-conversation")
        if self.keep_tab:
            args.append("--keep-tab")
        return tuple(args)
