"""Editable preset baselines.

| Preset     | Reading | Organizing+drafts | Sending | Permanent deletion | Additional |
|------------|---------|-------------------|---------|--------------------|------------|
| Reader     | Allow   | Deny              | Deny    | Deny               | Deny       |
| Assistant  | Allow   | Allow             | Ask     | Ask                | Ask        |
| Autonomous | Allow   | Allow             | Allow   | Allow              | Allow      |

New families are added here deliberately and announced in release notes.
"""

from __future__ import annotations

from ..domain.families import OperationFamily as F
from .model import Action, Preset

_ADDITIONAL = (
    F.ATTACHMENT_INGEST,
    F.EXPORT,
    F.IMPORT,
    F.FOLDER_DELETE,
    F.LABEL_DELETE,
    F.AUTOMATION,
)

PRESETS: dict[Preset, dict[F, Action]] = {
    Preset.READER: {
        F.READ: Action.ALLOW,
        F.ORGANIZE: Action.DENY,
        F.DRAFTS: Action.DENY,
        F.SEND: Action.DENY,
        F.PERMANENT_DELETE: Action.DENY,
        **{f: Action.DENY for f in _ADDITIONAL},
    },
    Preset.ASSISTANT: {
        F.READ: Action.ALLOW,
        F.ORGANIZE: Action.ALLOW,
        F.DRAFTS: Action.ALLOW,
        F.SEND: Action.ASK,
        F.PERMANENT_DELETE: Action.ASK,
        **{f: Action.ASK for f in _ADDITIONAL},
    },
    Preset.AUTONOMOUS: {f: Action.ALLOW for f in (F.READ, F.ORGANIZE, F.DRAFTS, F.SEND,
                                                  F.PERMANENT_DELETE, *_ADDITIONAL)},
}

AUTONOMOUS_WARNING = (
    "Autonomous mode lets the agent send, delete, import and export without asking. "
    "An agent that reads untrusted email can be manipulated by that email into sending "
    "or exporting information. You can add recipient/domain restrictions alongside this preset."
)


def baseline(preset: Preset, custom: dict[F, Action] | None = None) -> dict[F, Action]:
    if preset is Preset.CUSTOM:
        return dict(custom or {})
    return dict(PRESETS[preset])
