"""Bench Autopilot — stdlib-only bench orchestration CLI (GREEN)."""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key)\s*([:=]\s*|\s+)([^\s\"']+)"
)
SECRET_EQ_RE = re.compile(r"(?i)(--?(password|secret|token|api[_-]?key)=)(\S+)")


def redact(text):
    text = SECRET_RE.sub(r"\1=***REDACTED***", text)
    text = SECRET_EQ_RE.sub(r"\1***REDACTED***", text)
    return text


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_of(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def resolve_cmd(stage, config):
    stype = stage.get("type", "exec")
    if stype == "build":
        if stage.get("cmd"):
            return stage["cmd"]
        if config.get("build_cmd"):
            return config["build_cmd"]
        return [sys.executable, "-c", "print('dry-run: no build command configured')"]
    if stype == "manifest":
        return None
    return stage.get("cmd")


def run_exec_stage(name, cmd, timeout, tail_lines, cwd):
    started = utcnow()
    t0 = datetime.datetime.now(datetime.timezone.utc)
    if isinstance(cmd, str):
        popen_cmd, shell, disp = cmd, True, cmd
    else:
        popen_cmd, shell, disp = cmd, False, " ".join(str(c) for c in cmd)
    print(f"[autopilot] stage '{name}': $ {redact(disp)} (timeout={timeout}s)")
    proc = subprocess.Popen(
        popen_cmd, shell=shell, cwd=cwd or None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
            except Exception:
                pass
            out, _ = proc.communicate()
        exit_code = proc.returncode if proc.returncode is not None else 124
        out = (out or "") + f"\n[autopilot] TIMEOUT after {timeout}s (owned child killed)"
    ended = utcnow()
    duration = (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds()
    lines = redact(out or "").splitlines()
    tail = lines[-tail_lines:] if tail_lines > 0 else []
    status = "timeout" if timed_out else ("ok" if exit_code == 0 else "failed")
    print(f"[autopilot] stage '{name}': exit={exit_code} status={status}")
    for line in tail:
        print(f"  | {line}")
    return {
        "name": name, "type": "exec", "cmd": disp,
        "exit": exit_code, "status": status, "timed_out": timed_out,
        "started_at": started, "ended_at": ended,
        "duration_s": round(duration, 3), "tail": tail,
    }


def run_manifest_stage(name, artifacts, cwd, started=None):
    started = started or utcnow()
    t0 = datetime.datetime.now(datetime.timezone.utc)
    sha256s, missing = {}, []
    base = cwd or os.getcwd()
    for art in artifacts or []:
        p = art if os.path.isabs(art) else os.path.join(base, art)
        if os.path.isfile(p):
            try:
                sha256s[art] = sha256_of(p)
            except OSError as exc:
                missing.append(f"{art} (unreadable: {exc})")
        else:
            missing.append(art)
    ended = utcnow()
    duration = (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds()
    if missing:
        exit_code, status = 1, "failed"
        tail = [f"missing/unreadable artifacts: {', '.join(missing)}"]
    else:
        exit_code, status = 0, "ok"
        tail = [f"{k} sha256={v[:16]}..." for k, v in sha256s.items()] or ["no artifacts listed"]
    print(f"[autopilot] stage '{name}': exit={exit_code} status={status}")
    for line in tail:
        print(f"  | {line}")
    return {
        "name": name, "type": "manifest", "exit": exit_code, "status": status,
        "timed_out": False, "started_at": started, "ended_at": ended,
        "duration_s": round(duration, 3), "sha256s": sha256s,
        "missing": missing, "tail": tail,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bench Autopilot (stdlib only)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--receipt", default=None)
    ap.add_argument("--tail-lines", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=None)
    args = ap.parse_args(argv)
    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)
    cfg_dir = os.path.dirname(os.path.abspath(args.config))
    defaults = config.get("defaults", {}) or {}
    default_timeout = args.timeout or defaults.get("timeout", 60)
    default_tail = args.tail_lines if args.tail_lines is not None else defaults.get("tail_lines", 15)
    receipt_path = args.receipt or config.get("receipt", "receipt.json")
    if not os.path.isabs(receipt_path):
        receipt_path = os.path.join(cfg_dir, receipt_path)
    stages_cfg = config.get("stages", [])
    if not stages_cfg:
        print("[autopilot] no stages configured", file=sys.stderr)
        return 2
    results, ok = [], True
    for i, stage in enumerate(stages_cfg):
        name = stage.get("name", f"stage-{i + 1}")
        stype = stage.get("type", "exec")
        timeout = stage.get("timeout", default_timeout)
        tail_lines = stage.get("tail_lines", default_tail)
        cwd = stage.get("cwd") or cfg_dir
        if stype == "manifest":
            rec = run_manifest_stage(name, stage.get("artifacts", []), cwd)
        else:
            cmd = resolve_cmd(stage, config)
            if not cmd:
                rec = {"name": name, "type": stype, "exit": 2, "status": "failed",
                       "timed_out": False, "started_at": utcnow(), "ended_at": utcnow(),
                       "duration_s": 0.0, "tail": ["no command configured for stage"]}
            else:
                rec = run_exec_stage(name, cmd, timeout, tail_lines, cwd)
                rec["type"] = stype
        results.append(rec)
        if rec["exit"] != 0:
            ok = False
            for skipped in stages_cfg[i + 1:]:
                sname = skipped.get("name", "unnamed")
                results.append({"name": sname, "type": skipped.get("type", "exec"),
                                "exit": None, "status": "skipped", "timed_out": False,
                                "started_at": None, "ended_at": None, "duration_s": 0.0,
                                "tail": ["skipped: stop-on-first-failure"]})
                print(f"[autopilot] stage '{sname}': skipped (stop-on-first-failure)")
            break
    receipt = {"generated_at": utcnow(), "config": os.path.basename(args.config),
               "ok": ok, "stages": results}
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2)
    print(f"[autopilot] {'PASS' if ok else 'FAIL'} — receipt: {receipt_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
