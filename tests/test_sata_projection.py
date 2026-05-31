from app.services.sata_projection import project_multiple_horizons, project_sata_value


def test_sata_projection_compounds_and_adds_contributions():
    projection = project_sata_value(1000, 100, 1, annual_rate=0.13)
    assert projection.ending_value > 6200
    assert projection.total_contributions == 5200
    assert projection.annual_income_at_rate > 0


def test_multiple_horizons_shape():
    projections = project_multiple_horizons(500, 50)
    assert [p.years for p in projections] == [1, 3, 5]
