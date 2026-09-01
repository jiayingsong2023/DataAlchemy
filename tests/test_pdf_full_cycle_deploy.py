from scripts import run_pdf_full_cycle


def test_deploy_routes_each_application_image_to_its_role(monkeypatch):
    commands = []
    monkeypatch.setattr(run_pdf_full_cycle, "run", lambda command, **_: commands.append(command))
    monkeypatch.setattr(run_pdf_full_cycle, "_image_exists", lambda _: True)
    monkeypatch.setenv("DATAALCHEMY_WEB_IMAGE", "example/web:test")
    monkeypatch.setenv("DATAALCHEMY_HARNESS_IMAGE", "example/h5:test")
    monkeypatch.setenv("DATAALCHEMY_ETL_IMAGE", "example/etl:test")

    run_pdf_full_cycle.deploy("test-cluster")

    image_import = next(
        command for command in commands if command[:3] == ["k3d", "image", "import"]
    )
    assert image_import[3:6] == ["example/web:test", "example/h5:test", "example/etl:test"]
    helm = next(command for command in commands if command[:2] == ["helm", "upgrade"])
    assert "images.core=example/web:test" in helm
    assert "images.harnessJob=example/h5:test" in helm
    assert "images.etl=example/etl:test" in helm
