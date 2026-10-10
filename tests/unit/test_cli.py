import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import insert, text

from stewardry import __version__
from stewardry.cli import main
from stewardry.platform.audit import AuditEvent, AuditLog
from stewardry.platform.db.engine import make_engine
from stewardry.platform.db.schema import role_assignments, users
from stewardry.platform.redaction import Redactor


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"stewardry {__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: stewardry" in capsys.readouterr().out


def test_db_upgrade_creates_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STEWARDRY_DATA_DIR", str(tmp_path / "data"))
    assert main(["db", "upgrade"]) == 0
    assert (tmp_path / "data" / "stewardry.db").is_file()
    assert "latest schema" in capsys.readouterr().out


def test_db_upgrade_refuses_an_unsafe_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("STEWARDRY_DATA_DIR", str(tmp_path / "data"))
    assert main(["db", "upgrade"]) == 2
    assert "inside the git work tree" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def _migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("STEWARDRY_DATA_DIR", str(data))
    assert main(["db", "upgrade"]) == 0
    return data


def _append_events(data: Path, count: int) -> None:
    engine = make_engine(data / "stewardry.db")
    with engine.begin() as conn:
        for _ in range(count):
            AuditLog(Redactor()).append(conn, AuditEvent("platform.test", "succeeded"))
    engine.dispose()


def test_audit_verify_and_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _migrated(tmp_path, monkeypatch)
    _append_events(data, 2)
    capsys.readouterr()

    assert main(["audit", "verify"]) == 0
    out = capsys.readouterr().out
    assert "2 event(s)" in out and "audit: OK" in out and "after the newest anchor" in out

    assert main(["audit", "anchor"]) == 0
    assert "anchored seq 2" in capsys.readouterr().out
    assert (data / "audit-anchors.jsonl").is_file()

    assert main(["audit", "verify"]) == 0
    out = capsys.readouterr().out
    assert "1 anchor(s) checked" in out and "after the newest anchor" not in out


def test_audit_verify_reports_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _migrated(tmp_path, monkeypatch)
    _append_events(data, 2)
    engine = make_engine(data / "stewardry.db")
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER audit_events_no_update"))
        conn.execute(text("UPDATE audit_events SET outcome = 'denied' WHERE seq = 1"))
    engine.dispose()
    capsys.readouterr()

    assert main(["audit", "verify"]) == 1
    out = capsys.readouterr().out
    assert "seq 1: content does not match its hash" in out and "audit: FAILED" in out


def test_audit_commands_need_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STEWARDRY_DATA_DIR", str(tmp_path / "data"))
    assert main(["audit", "verify"]) == 2
    assert main(["audit", "anchor"]) == 2
    assert "run: stewardry db upgrade" in capsys.readouterr().err


def test_audit_anchor_refuses_an_empty_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _migrated(tmp_path, monkeypatch)
    assert main(["audit", "anchor", "--anchor-file", str(tmp_path / "a.jsonl")]) == 2
    assert "empty" in capsys.readouterr().err


def test_audit_verify_refuses_an_unsafe_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("STEWARDRY_DATA_DIR", str(tmp_path / "data"))
    assert main(["audit", "verify"]) == 2


AGENT_FILE = str(Path(__file__).resolve().parents[2] / "policies/agents/questionnaire-agent.yaml")
BASELINE_FILE = str(Path(__file__).resolve().parents[2] / "policies/platform-baseline.yaml")


def _seed_people(data: Path) -> None:
    now = dt.datetime(2026, 10, 10, tzinfo=dt.UTC)
    engine = make_engine(data / "stewardry.db")
    with engine.begin() as conn:
        for sub, name, role in [
            ("sub-platform", "platform", "platform_engineer"),
            ("sub-owner", "owner", "engagement_owner"),
        ]:
            conn.execute(
                insert(users).values(sub=sub, username=name, display_name=name, created_at=now)
            )
            conn.execute(
                insert(role_assignments).values(
                    sub=sub, role=role, assigned_by_sub=sub, assigned_at=now
                )
            )
    engine.dispose()


def test_registry_commands_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _migrated(tmp_path, monkeypatch)
    _seed_people(data)
    plat, owner = ["--as", "sub-platform"], ["--as", "sub-owner"]
    agent = "questionnaire-agent"
    capsys.readouterr()

    assert main(["registry", "apply", AGENT_FILE, "--baseline", BASELINE_FILE, *plat]) == 0
    assert "new version, version 1); re-evaluation required: True" in capsys.readouterr().out
    assert main(["registry", "apply", AGENT_FILE, "--baseline", BASELINE_FILE, *plat]) == 0
    assert "(unchanged, version 1)" in capsys.readouterr().out

    assert main(["registry", "transition", agent, "build", "--evidence", "brief.md", *owner]) == 0
    assert main(["registry", "show", agent]) == 0
    out = capsys.readouterr().out
    assert "questionnaire-agent: build, enabled" in out
    policy_hash = out.split("policy ")[1].split(" ")[0]

    assert main(["registry", "transition", agent, "shadow", "--evidence", "g.md", *owner]) == 1
    assert "accepted evaluation" in capsys.readouterr().err
    assert (
        main(
            [
                "registry",
                "accept-eval",
                agent,
                "--policy-hash",
                policy_hash,
                "--report",
                "evals/r1.md",
                *owner,
            ]
        )
        == 0
    )
    assert main(["registry", "transition", agent, "shadow", "--evidence", "g.md", *owner]) == 0

    for action, expected in [("pause", "paused"), ("resume", "resumed"), ("retire", "retired")]:
        assert main(["registry", action, agent, "--reason", "test", *plat]) == 0
        assert f"{agent}: {expected}" in capsys.readouterr().out

    assert main(["audit", "verify"]) == 0


def test_registry_refusals_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _migrated(tmp_path, monkeypatch)
    _seed_people(data)
    capsys.readouterr()
    assert (
        main(["registry", "apply", AGENT_FILE, "--baseline", BASELINE_FILE, "--as", "sub-owner"])
        == 1
    )
    assert "requires the platform_engineer role" in capsys.readouterr().err
    assert main(["registry", "apply", str(tmp_path / "missing.yaml"), "--as", "sub-platform"]) == 1
    assert "cannot read YAML" in capsys.readouterr().err
    assert main(["registry", "show", "ghost-agent"]) == 2
    assert "not registered" in capsys.readouterr().err


def test_registry_schema_and_missing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["registry", "schema"]) == 0
    assert '"title": "AgentPolicyFile"' in capsys.readouterr().out
    monkeypatch.setenv("STEWARDRY_DATA_DIR", str(tmp_path / "data"))
    assert main(["registry", "show", "questionnaire-agent"]) == 2
