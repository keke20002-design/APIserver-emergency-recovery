from services.dashboard_db import _merge_campaign_catalog
from services.google_ads_service import _target_install_cost_micros


def test_only_install_cost_goals_are_treated_as_tcpi():
    assert _target_install_cost_micros("OPTIMIZE_INSTALLS_TARGET_INSTALL_COST", 395_000_000, 0) == 395_000_000
    assert _target_install_cost_micros("OPTIMIZE_IN_APP_CONVERSIONS_TARGET_INSTALL_COST", 0, 375_000_000) == 375_000_000
    assert _target_install_cost_micros("OPTIMIZE_IN_APP_CONVERSIONS_TARGET_CONVERSION_COST", 395_000_000, 0) is None
    assert _target_install_cost_micros("OPTIMIZE_RETURN_ON_ADVERTISING_SPEND", 395_000_000, 0) is None


def test_campaign_sync_preserves_app_mapping_and_unchanged_target_date():
    previous_date = "2026-09-01T00:00:00+00:00"
    now = "2026-09-23T00:00:00+00:00"
    existing = [{
        "campaign_id": "123",
        "campaign_name": "Old name",
        "app_id": "my-app",
        "active": True,
        "target_cpi": "395.0000",
        "daily_budget": "10000.0000",
        "target_cpi_changed_at": previous_date,
    }]
    incoming = [{
        "campaign_id": "123",
        "campaign_name": "New name",
        "active": True,
        "target_cpi": 395.0,
        "daily_budget": 12000.0,
    }]

    result = _merge_campaign_catalog(existing, incoming, now)[0]

    assert result["app_id"] == "my-app"
    assert result["campaign_name"] == "New name"
    assert result["daily_budget"] == 12000.0
    assert result["target_cpi_changed_at"] == previous_date
    assert result["settings_synced_at"] == now


def test_campaign_sync_records_target_change_for_cooldown():
    now = "2026-09-23T00:00:00+00:00"
    existing = [{
        "campaign_id": "123",
        "campaign_name": "Campaign",
        "app_id": "my-app",
        "target_cpi": 395.0,
        "target_cpi_changed_at": "2026-09-01T00:00:00+00:00",
        "settings_synced_at": "2026-09-20T00:00:00+00:00",
    }]
    incoming = [{
        "campaign_id": "123",
        "campaign_name": "Campaign",
        "target_cpi": 375.0,
        "daily_budget": 10000.0,
    }]

    result = _merge_campaign_catalog(existing, incoming, now)[0]

    assert result["app_id"] == "my-app"
    assert result["target_cpi_changed_at"] == now


def test_currency_conversion_drift_does_not_fake_a_target_change():
    previous_date = "2026-09-20T00:00:00+00:00"
    now = "2026-10-11T00:00:00+00:00"
    existing = [{
        "campaign_id": "123",
        "campaign_name": "Campaign",
        "target_cpi": 0.2818,
        "daily_budget": 3.3542,
        "target_cpi_changed_at": previous_date,
        "settings_synced_at": "2026-10-10T00:00:00+00:00",
    }]
    incoming = [{
        "campaign_id": "123",
        "campaign_name": "Campaign",
        "target_cpi": 0.2830,
        "daily_budget": 3.3685,
    }]

    result = _merge_campaign_catalog(existing, incoming, now)[0]
    assert result["target_cpi_changed_at"] == previous_date
    assert result["target_cpi"] == 0.2830


def test_first_settings_observation_does_not_invent_a_change_date():
    now = "2026-09-23T00:00:00+00:00"
    existing = [{
        "campaign_id": "123",
        "campaign_name": "Campaign",
        "app_id": "my-app",
    }]
    incoming = [{
        "campaign_id": "123",
        "campaign_name": "Campaign",
        "target_cpi": 395.0,
        "daily_budget": 10000.0,
    }]

    result = _merge_campaign_catalog(existing, incoming, now)[0]

    assert result["target_cpi_changed_at"] is None
    assert result["settings_synced_at"] == now
