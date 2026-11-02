from betbot import setup_wizard
from betbot.config import Config
from betbot.setup_wizard import Answers, build_config


def test_live_tennis_at_ozoon_at_max():
    cfg = build_config(Answers(account="lee-office", books=["OZ"], leagues=["ATP", "WTA"], live_only=True,
                               stake=None, max_per_event=5000, daily_cap=20000))
    assert cfg.books["OZ"].enabled and not cfg.books["MyB"].enabled
    assert cfg.books["OZ"].account == cfg.books["MyB"].account == "lee-office"
    assert (cfg.only.leagues, cfg.only.live) == (["ATP", "WTA"], True)
    assert (cfg.stake.mode, cfg.stake.max_per_event, cfg.stake.daily_cap) == ("max", 5000, 20000)
    assert cfg.one_bet_per_market and cfg.dry_run
    assert "https://arbhunterpro.com/json?atp-pro" in cfg.feeds


def test_fixed_stake_everything():
    cfg = build_config(Answers(account="pc2", books=["OZ", "MyB"], stake=50))
    assert cfg.books["OZ"].enabled and cfg.books["MyB"].enabled
    assert (cfg.stake.mode, cfg.stake.default, cfg.only.leagues, cfg.only.live) == ("fixed", 50, [], False)


def test_the_questions_write_config_and_ask_for_logins(tmp_path, monkeypatch):
    replies = iter(["lee-office", "1", "2", "max", "", "", "y"])  # Enter keeps the defaults
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    logins = []
    monkeypatch.setattr(setup_wizard, "get_credentials", lambda book, account: None)
    monkeypatch.setattr(setup_wizard, "prompt_and_store", lambda book, account: logins.append((book, account)))
    p = tmp_path / "config.yaml"
    setup_wizard.run(p)
    cfg = Config.load(p)
    assert cfg.books["OZ"].enabled and not cfg.books["MyB"].enabled
    assert (cfg.only.leagues, cfg.only.live, cfg.stake.mode) == (["ATP", "WTA"], True, "max")
    assert (cfg.stake.max_per_event, cfg.stake.daily_cap) == (5000, 20000)
    assert logins == [("OZ", "lee-office")]
    assert cfg.add_new_feeds() == []  # nothing left for the start-up feed migration to add


def test_bad_answers_are_asked_again(monkeypatch):
    replies = iter(["9", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    assert setup_wizard._choose("?", {"1": "a", "2": "b"}, "1") == "2"
    replies2 = iter(["lots", "$1,500"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies2))
    assert setup_wizard._money("?", "50") == 1500
