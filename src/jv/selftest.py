"""Catalogue lint: `python -m jv.selftest` prints problems (exit 1) or `ok N verbs`."""
import re
import sys

PROP_TYPES = {"string", "integer", "boolean"}
ARG_TYPES = {"int", "str", "enum", "text"}
SLOT = re.compile(r"\{(\w+):(\w+)\}")


def problems(cat: dict) -> list[str]:
    out, seen, phrase_of = [], set(), {}
    for v in cat["verbs"]:
        n = v.get("name")
        bad = lambda msg: out.append(f"{n}: {msg}")
        if n in seen:
            bad("duplicate name")
        seen.add(n)
        d, vb = v.get("domain"), v.get("verb")
        if n != (d if d == vb else f"{d}.{vb}"):
            bad(f"name does not match domain {d!r} / verb {vb!r}")
        params = v.get("params", {})
        props = params.get("properties", {})
        if params.get("type") != "object":
            bad("params.type must be object")
        for k, p in props.items():
            if p.get("type") not in PROP_TYPES:
                bad(f"property {k} has type {p.get('type')!r}")
        keys = [a.removeprefix("--").replace("-", "_") for a in v.get("argv", [])]
        for k in keys:
            if k not in props:
                bad(f"argv {k} not in params")
        for k in params.get("required", []):
            if k not in keys:
                bad(f"required {k} not in argv")
        if v.get("confirm") not in (True, False, "auto"):
            bad(f"confirm {v.get('confirm')!r}")
        positional = [a.replace("-", "_") for a in v.get("argv", []) if not a.startswith("--")]
        args = v.get("args", {})
        for k, s in args.items():
            t = s.get("type")
            if k not in props:
                bad(f"args {k} not in params")
            if t not in ARG_TYPES:
                bad(f"args {k} has type {t!r}")
            if t == "enum" and not isinstance(s.get("choices"), list):
                bad(f"args {k} enum needs a choices list")
            if t == "text" and (not positional or positional[-1] != k):
                bad(f"args {k} text must be the last positional")
        for ph in v.get("phrases", []):
            if phrase_of.setdefault(ph, n) != n:
                bad(f"phrase {ph!r} already used by {phrase_of[ph]}")
            for k, t in SLOT.findall(ph):
                if args.get(k, {}).get("type") != t:
                    bad(f"phrase slot {{{k}:{t}}} does not match args")
        for ph, fixed in v.get("phrase_args", {}).items():
            if ph not in v.get("phrases", []) or not set(fixed) <= set(props):
                bad(f"phrase_args {ph!r} needs a phrase and params")
        if not isinstance(v.get("voice"), bool):
            bad("voice must be bool")
    return out


def main() -> int:
    from jv.cli import all_verbs, catalogue
    cat = catalogue(all_verbs())
    ps = problems(cat)
    print("\n".join(ps) if ps else f"ok {len(cat['verbs'])} verbs")
    return 1 if ps else 0


if __name__ == "__main__":
    sys.exit(main())
