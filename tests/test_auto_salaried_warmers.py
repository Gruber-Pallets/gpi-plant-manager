from zira_dashboard import app as app_module


def _warmer(name):
    return next((w for w in app_module._WARMERS if w[0] == name), None)


def test_auto_salaried_warmers_registered():
    tick = _warmer("auto-salaried punch")
    reconcile = _warmer("auto-salaried reconcile")
    assert tick is not None and tick[2] == 60
    assert reconcile is not None and reconcile[2] == 600
