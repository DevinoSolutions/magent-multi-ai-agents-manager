from magent.discover import discover_projects


class TestThreeWayMerge:
    """Drives discover_projects's three-way merge deterministically by
    monkeypatching the three source discoverers, independent of filesystem
    mtimes. Sibling file to test_discover.py so it does NOT inherit that
    file's autouse vscode stub -- the vscode source is exercised here."""

    def _seed(self, monkeypatch, tmp_path, codex_active, vscode_active, claude_active):
        # Deep enough to pass _is_real_project on both Win (>=5 parts) and
        # Unix (>=4 parts); leaf not in GENERIC_DIRS.
        project_dir = tmp_path / "acme" / "myapp"
        project_dir.mkdir(parents=True)
        p = str(project_dir)

        import magent.discover as discover_mod

        monkeypatch.setattr(
            discover_mod,
            "_discover_codex_projects",
            lambda home=None: [
                {
                    "path": p,
                    "tool": "codex",
                    "session_count": 1,
                    "last_active": codex_active,
                }
            ],
        )
        monkeypatch.setattr(
            discover_mod,
            "_discover_vscode_projects",
            lambda: [
                {
                    "path": p,
                    "tool": "vscode",
                    "session_count": 1,
                    "last_active": vscode_active,
                }
            ],
        )
        monkeypatch.setattr(
            discover_mod,
            "_claude_sessions_for_path",
            lambda path, home=None: {"session_count": 1, "last_active": claude_active},
        )

    def test_claude_wins_when_genuinely_newest(self, monkeypatch, tmp_path):
        """GREEN, positive: claude is the newest of all three -> guards E7 from
        regressing the correct claude-preference case."""
        self._seed(
            monkeypatch, tmp_path, codex_active=250, vscode_active=90, claude_active=300
        )

        projects, _ = discover_projects(home=tmp_path)

        assert len(projects) == 1
        assert projects[0]["tool"] == "claude"
        assert projects[0]["last_active"] == 300

    def test_three_way_merge_newest_source_wins_R9(self, monkeypatch, tmp_path):
        """Pins the CORRECT outcome (codex/250 is the newest source overall).
        Was xfail: the old merge compared claude only against the current
        source, not the running best (discover.py:208), so the vscode(90)
        pass re-compared claude(150) only against itself and wrongly beat
        the stored codex(250) best. Fixed by _merge_candidate offering every
        candidate through one max-keyed comparison (R9) -- this is now the
        one authoritative pin for the three-way merge (no separate
        test_merge_three_sources_keeps_max_last_active; would duplicate)."""
        self._seed(
            monkeypatch, tmp_path, codex_active=250, vscode_active=90, claude_active=150
        )

        projects, _ = discover_projects(home=tmp_path)

        assert len(projects) == 1
        assert projects[0]["tool"] == "codex"
        assert projects[0]["last_active"] == 250
