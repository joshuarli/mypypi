import pytest

from mypypi.cli import main


def test_help_lists_all_commands(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    for command in ("add", "plan", "build", "validate", "publish"):
        assert command in help_text


@pytest.mark.parametrize("command", ("add", "plan", "build", "validate", "publish"))
def test_subcommand_help(command, capsys):
    with pytest.raises(SystemExit) as result:
        main([command, "--help"])
    assert result.value.code == 0
    assert command in capsys.readouterr().out


def test_publish_help_requires_output(capsys):
    with pytest.raises(SystemExit) as result:
        main(["publish", "--help"])
    assert result.value.code == 0
    assert "--output" in capsys.readouterr().out


def test_command_error_reports_python_and_stage(tmp_path, capsys):
    catalog = tmp_path / "packages.toml"
    catalog.write_text("wrong = []\n", encoding="utf-8")

    status = main(["--catalog", str(catalog), "plan"])

    assert status == 1
    diagnostic = capsys.readouterr().err
    assert "Python 3.15" in diagnostic
    assert "stage configuration" in diagnostic


def test_platform_selection_uses_the_configured_target_list(capsys):
    status = main(["plan", "--platform", "windows-x86_64"])

    assert status == 1
    diagnostic = capsys.readouterr().err
    assert "unconfigured platform" in diagnostic
    assert "Python 3.15" in diagnostic


def test_strict_build_dependency_mode_requires_a_curated_index(capsys):
    status = main(["build", "--strict-build-deps"])

    assert status == 1
    diagnostic = capsys.readouterr().err
    assert "curated build dependency index" in diagnostic


def test_keyboard_interrupt_returns_cancelled_status_without_traceback(
    monkeypatch, capsys
):
    import mypypi.cli as cli

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_command_plan", interrupt)

    assert main(["plan"]) == 130
    assert capsys.readouterr().err == ""
