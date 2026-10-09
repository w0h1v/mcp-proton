"""Policy evaluation. Pure function of (config, caller, request, counters)."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..domain.families import OperationFamily
from ..domain.requests import CallerContext, OperationRequest
from .model import ACTION_SEVERITY, Action, Constraints, PolicyConfig, PolicyRule
from .presets import baseline


@dataclass
class Decision:
    action: Action
    family: OperationFamily
    reasons: list[str] = field(default_factory=list)  # human-readable sources
    violation: str | None = None  # constraint that forced Deny, if any
    code: str | None = None  # error code to use when denied

    @property
    def allowed(self) -> bool:
        return self.action is Action.ALLOW


def _mailbox_matches(pattern: str, mailbox: str) -> bool:
    if pattern.endswith("/*"):
        prefix = pattern[:-2]
        return mailbox == prefix or mailbox.startswith(prefix + "/")
    return fnmatch.fnmatchcase(mailbox, pattern)


def _rule_matches(rule: PolicyRule, client: str, account: str, mailbox: str | None,
                  family: OperationFamily, now: datetime) -> bool:
    if rule.family != family or not rule.active(now):
        return False
    if rule.client is not None and rule.client != client:
        return False
    if rule.account is not None and rule.account != account:
        return False
    if rule.mailbox is not None:
        return mailbox is not None and _mailbox_matches(rule.mailbox, mailbox)
    return True


def resolve_action(config: PolicyConfig, client: str, account: str, mailbox: str | None,
                   family: OperationFamily, now: datetime | None = None) -> tuple[Action, str]:
    """Effective action for one scope plus a description of its source."""
    now = now or datetime.now(UTC)
    if config.preset is None:
        return Action.DENY, "not onboarded: no preset selected"
    base = baseline(config.preset, config.custom_baseline)
    action = base.get(family)
    source = f"preset {config.preset.value}"
    if action is None:
        action, source = Action.DENY, f"family {family.value} not classified in preset"
    matches = [r for r in config.rules if _rule_matches(r, client, account, mailbox, family, now)]
    if matches:
        top = max(r.specificity() for r in matches)
        best = [r for r in matches if r.specificity() == top]
        winner = max(best, key=lambda r: ACTION_SEVERITY[r.action])
        action = winner.action
        sel = ",".join(
            f"{k}={v}" for k, v in (("client", winner.client), ("account", winner.account),
                                    ("mailbox", winner.mailbox)) if v is not None
        ) or "global"
        kind = "temporary grant" if winner.expires_at else "rule"
        source = f"{kind} [{sel}]"
    return action, source


def _intersect_constraints(config: PolicyConfig, client: str) -> list[Constraints]:
    out = [config.constraints]
    cc = config.client(client)
    if cc is not None and cc.constraints is not None:
        out.append(cc.constraints)
    return out


def _recipient_allowed(addr: str, allowed: Iterable[str]) -> bool:
    addr = addr.lower()
    for entry in allowed:
        entry = entry.lower()
        if entry.startswith("@"):
            if addr.endswith(entry):
                return True
        elif addr == entry:
            return True
    return False


def path_within(path: str, roots: Iterable[str]) -> bool:
    """True if the resolved path is inside one of the resolved roots."""
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    for root in roots:
        r = os.path.realpath(os.path.expanduser(root))
        if real == r or real.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


_PATH_DIRS = {
    OperationFamily.ATTACHMENT_INGEST: "attachment_ingest_dirs",
    OperationFamily.EXPORT: "export_dirs",
    OperationFamily.IMPORT: "import_dirs",
}


def check_constraints(config: PolicyConfig, caller: CallerContext, req: OperationRequest,
                      sends_today: int = 0) -> str | None:
    """Return a violation description, or None. Path constraints for draft
    attachments are checked by passing ``family=ATTACHMENT_INGEST`` paths via
    ``req.paths`` together with ``payload['_path_family']``."""
    for c in _intersect_constraints(config, caller.client_id):
        if c.allowed_accounts is not None and req.account not in c.allowed_accounts:
            return f"account {req.account!r} outside allowed accounts"
        if c.allowed_mailboxes is not None:
            allowed = c.allowed_mailboxes.get(req.account)
            if allowed is None:
                return f"no mailboxes allowed for account {req.account!r}"
            for mb in req.mailboxes:
                if not any(_mailbox_matches(p, mb) for p in allowed):
                    return f"mailbox {mb!r} outside allowed mailboxes"
        if c.max_batch_size is not None and req.batch_size > c.max_batch_size:
            return f"batch size {req.batch_size} exceeds limit {c.max_batch_size}"
        if req.family is OperationFamily.SEND:
            if c.allowed_recipients is not None:
                bad = [r for r in req.recipients if not _recipient_allowed(r, c.allowed_recipients)]
                if bad:
                    return f"recipients outside allowed list: {', '.join(sorted(bad))}"
            if c.max_sends_per_day is not None and sends_today >= c.max_sends_per_day:
                return f"daily send limit {c.max_sends_per_day} reached"
        if req.paths:
            path_family = OperationFamily(req.payload.get("_path_family", req.family))
            attr = _PATH_DIRS.get(path_family)
            roots = getattr(c, attr) if attr else []
            for p in req.paths:
                if not path_within(p, roots):
                    return f"path outside configured {path_family.value} directories"
    return None


def evaluate(config: PolicyConfig, caller: CallerContext, req: OperationRequest,
             sends_today: int = 0, now: datetime | None = None) -> Decision:
    now = now or datetime.now(UTC)
    client_cfg = config.client(caller.client_id)
    if client_cfg is not None and client_cfg.revoked:
        return Decision(Action.DENY, req.family, ["client revoked"], code="client_revoked")
    if req.account in config.paused_accounts:
        return Decision(Action.DENY, req.family, ["account paused"], code="account_paused")
    if config.preset is None:
        return Decision(Action.DENY, req.family, ["not onboarded: no preset selected"],
                        code="not_onboarded")

    scopes: list[str | None] = list(req.mailboxes) or [None]
    worst = Action.ALLOW
    reasons: list[str] = []
    for mb in scopes:
        action, source = resolve_action(config, caller.client_id, req.account, mb, req.family, now)
        reasons.append(f"{req.family.value}@{mb or '*'}: {action.value} ({source})")
        if ACTION_SEVERITY[action] > ACTION_SEVERITY[worst]:
            worst = action
    # Extra families implied by the request (e.g. a draft that ingests local files,
    # a folder deletion that destroys messages) must also pass.
    for extra in req.payload.get("_also_families", []):
        fam = OperationFamily(extra)
        for mb in scopes:
            action, source = resolve_action(config, caller.client_id, req.account, mb, fam, now)
            reasons.append(f"{fam.value}@{mb or '*'}: {action.value} ({source})")
            if ACTION_SEVERITY[action] > ACTION_SEVERITY[worst]:
                worst = action
    if worst is Action.DENY:
        return Decision(Action.DENY, req.family, reasons, code="policy_denied")
    violation = check_constraints(config, caller, req, sends_today)
    if violation:
        code = "path_rejected" if "path" in violation else "constraint_violation"
        if "limit" in violation and "batch" not in violation:
            code = "limit_exceeded"
        return Decision(Action.DENY, req.family, reasons, violation=violation, code=code)
    return Decision(worst, req.family, reasons)


def explain(config: PolicyConfig, client: str, account: str, mailbox: str | None = None
            ) -> dict[str, dict[str, str]]:
    """Effective setting and its source for every family (shown before use)."""
    out: dict[str, dict[str, str]] = {}
    for fam in OperationFamily:
        action, source = resolve_action(config, client, account, mailbox, fam)
        out[fam.value] = {"action": action.value, "source": source}
    return out
