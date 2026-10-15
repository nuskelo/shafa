"""Встановлення й оновлення: замінюється лише тека програми, дані — ні (WIN-001 D6)."""

import pytest

from shafa import install


def fake_app(path, version):
    (path / "python").mkdir(parents=True)
    (path / "python" / "pythonw.exe").write_bytes(b"MZ")
    (path / "lib" / "shafa").mkdir(parents=True)
    (path / "lib" / "shafa" / "__init__.py").write_text(f"# {version}\n", encoding="utf-8")
    (path / "VERSION").write_text(version, encoding="utf-8")
    return path


@pytest.fixture
def data(tmp_path):
    """Тека даних з усім, що в ній буває, — з кирилицею."""
    data = tmp_path / "Вікторія" / "AppData" / "Local" / "shafa"
    data.mkdir(parents=True)
    (data / ".env").write_text("ANTHROPIC_API_KEY=секрет\n", encoding="utf-8")
    (data / "shafa.sqlite").write_bytes(b"SQLite format 3\x00")
    for folder in ("thumbs", "robot-profile", "screenshots"):
        (data / folder).mkdir()
        (data / folder / "файл").write_text("x", encoding="utf-8")
    (data / "app.log").write_text("журнал", encoding="utf-8")
    return data


def snapshot(data):
    return {
        p.relative_to(data).as_posix(): p.read_bytes()
        for p in data.rglob("*")
        if p.is_file() and p.relative_to(data).parts[0] not in ("app", "app.old")
    }


def test_first_install(tmp_path, data):
    target = install.install(fake_app(tmp_path / "нова" / "app", "1"), data)
    assert target == data / "app"
    assert (target / "VERSION").read_text(encoding="utf-8") == "1"


def test_update_replaces_only_program(tmp_path, data):
    install.install(fake_app(tmp_path / "v1" / "app", "1"), data)
    (data / "app" / "lib" / "shafa" / "старий.py").write_text("x", encoding="utf-8")
    before = snapshot(data)

    install.install(fake_app(tmp_path / "v2" / "app", "2"), data)

    assert (data / "app" / "VERSION").read_text(encoding="utf-8") == "2"
    assert not (data / "app" / "lib" / "shafa" / "старий.py").exists()
    assert snapshot(data) == before  # .env, база, мініатюри, профіль, журнали — ті самі
    assert not (data / "app.old").exists()


def test_failed_copy_keeps_old_version(tmp_path, data, monkeypatch):
    install.install(fake_app(tmp_path / "v1" / "app", "1"), data)

    def broken(src, dst):
        (dst / "частково").mkdir(parents=True)
        raise OSError("диск заповнено")

    monkeypatch.setattr(install.shutil, "copytree", broken)
    with pytest.raises(install.InstallError, match="диск заповнено"):
        install.install(fake_app(tmp_path / "v2" / "app", "2"), data)
    assert (data / "app" / "VERSION").read_text(encoding="utf-8") == "1"
    assert not (data / "app.old").exists()


def test_running_program_explained(tmp_path, data, monkeypatch):
    install.install(fake_app(tmp_path / "v1" / "app", "1"), data)
    real = install.Path.rename

    def locked(self, target):
        if self.name == "app":
            raise PermissionError("файл зайнятий")
        return real(self, target)

    monkeypatch.setattr(install.Path, "rename", locked)
    with pytest.raises(install.InstallError, match="Програма ще працює"):
        install.install(fake_app(tmp_path / "v2" / "app", "2"), data)
    assert (data / "app" / "VERSION").read_text(encoding="utf-8") == "1"


def test_not_a_program_or_installed_itself(tmp_path, data):
    with pytest.raises(install.InstallError, match="немає програми"):
        install.install(tmp_path, data)
    install.install(fake_app(tmp_path / "v1" / "app", "1"), data)
    with pytest.raises(install.InstallError, match="з розпакованої нової версії"):
        install.install(data / "app", data)


def test_main_reports_and_skips_wizard_on_update(tmp_path, data, monkeypatch, capsys):
    monkeypatch.setenv("SHAFA_DATA_DIR", str(data))
    monkeypatch.setattr(install.config, "ENV_FILE", None)
    ran = []
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: ran.append(a[0]))

    code = install.main([str(fake_app(tmp_path / "v1" / "app", "1.2"))])

    out = capsys.readouterr().out
    assert code == 0
    assert "Встановлено «Шафа-помічник» 1.2" in out and "їх не чіпали" in out
    assert ran == []  # .env уже є — майстер не потрібен; старої програми не було — quit не кличемо
