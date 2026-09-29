from conftest import write_prepared

from app.catalog import Channel, add_external_catalogs


def test_catalog_reads_hierarchy_and_multiline_names(catalog):
    assert len(catalog.objects) == 4
    assert len(catalog.channels) == 4
    # родителя района в справочнике нет — район становится корнем
    assert [o.id for o in catalog.roots()] == [5773]
    assert [o.id for o in catalog.children(7)] == [5961, 5962]
    # перенос строки внутри кавычек не ломает запись
    assert catalog.channels[120298].name == "Темп. ВШ ПК88,5"
    assert catalog.channels[120298].object_id == 5962


def test_picket_from_name():
    def picket(name: str) -> str:
        return Channel(1, 1, "", "", "", name).picket

    assert picket("ДД ПК12") == "12"
    assert picket("ТД ПК86-85") == "86-85"
    assert picket("Темп. ВШ ПК88,5") == "88,5"
    assert picket("ГД ПК 7") == "7"
    assert picket("ГД ВШ") == ""


def test_channel_ids_by_object_scope(catalog):
    assert catalog.channel_ids() == {287267, 287268, 120298, 300001}
    assert catalog.channel_ids([7]) == {287267, 287268, 120298, 300001}
    assert catalog.channel_ids([5961]) == {287267, 287268}


def test_external_catalog_merges_as_separate_root(catalog, tmp_path):
    write_prepared(tmp_path / "external" / "magdeburg")
    assert add_external_catalogs(catalog, tmp_path / "external") == ["magdeburg"]
    roots = {o.id: o.dataset for o in catalog.roots()}
    assert roots == {5773: None, 9010001: "magdeburg"}
    assert catalog.channel_ids([9010002]) == {901000600, 901000601, 901000602}
    assert catalog.channels[901000601].sensor_type == "Датчик CO₂" and catalog.channels[901000601].tag == "CO2_Room"
    assert catalog.objects[9010106].dataset == "magdeburg" and catalog.objects[7].dataset is None

    # набор с id, уже занятыми в справочнике, не вливается
    clash = tmp_path / "clash" / "copy"
    write_prepared(clash)
    assert add_external_catalogs(catalog, tmp_path / "clash") == []
    assert len(catalog.channels) == 7
