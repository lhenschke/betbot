import yaml

from betbot.config import DEFAULT_FEEDS, Config

NCAAB = "https://arbhunterpro.com/json?ncaab-pro"
TENNIS = "https://arbhunterpro.com/json?atp-pro"


def test_new_feeds_reach_an_existing_config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"feeds": DEFAULT_FEEDS[:4]}))  # a config.yaml from before NCAAB and tennis
    cfg = Config.load(p)
    assert cfg.add_new_feeds() == [NCAAB, TENNIS]
    assert cfg.feeds == DEFAULT_FEEDS
    cfg.save(p)
    assert Config.load(p).add_new_feeds() == []  # only once


def test_a_removed_feed_stays_removed(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"feeds": DEFAULT_FEEDS[:4]}))
    cfg = Config.load(p)
    cfg.add_new_feeds()
    cfg.feeds.remove(TENNIS)
    cfg.save(p)
    cfg = Config.load(p)
    assert cfg.add_new_feeds() == [] and TENNIS not in cfg.feeds


def test_example_config_needs_no_additions(tmp_path):
    cfg = Config.load("config.example.yaml")
    assert set(DEFAULT_FEEDS) <= set(cfg.feeds)
    before = list(cfg.feeds)
    cfg.add_new_feeds()
    assert cfg.feeds == before
