from tilequeue.query.fixture import make_fixture_data_fetcher
from tilequeue.query.pool import DBConnectionPool
from tilequeue.query.postgres import make_db_data_fetcher
from tilequeue.query.rawr import make_rawr_data_fetcher
from tilequeue.query.split import make_split_data_fetcher
from tilequeue.process import Source


__all__ = [
    'DBConnectionPool',
    'make_db_data_fetcher',
    'make_fixture_data_fetcher',
    'make_data_fetcher',
]


def make_data_fetcher(cfg, layer_data, query_cfg, io_pool):
    db_fetcher = make_db_data_fetcher(
        cfg.postgresql_conn_info, cfg.template_path, cfg.reload_templates,
        query_cfg, io_pool)

    if cfg.yml.get('use-rawr-tiles'):
        rawr_fetcher = _make_rawr_fetcher(
            cfg, layer_data, query_cfg, io_pool)

        group_by_zoom = cfg.yml.get('rawr').get('group-zoom')
        assert group_by_zoom is not None, 'Missing group-zoom rawr config'
        return make_split_data_fetcher(
            group_by_zoom, db_fetcher, rawr_fetcher)

    else:
        return db_fetcher


class _NullRawrStorage(object):

    def __init__(self, data_source, table_sources):
        self.data_source = data_source
        self.table_sources = table_sources

    def __call__(self, tile):
        # returns a "tables" object, which responds to __call__(table_name)
        # with tuples for that table.
        data = {}
        for location in self.data_source(tile):
            data[location.name] = location.records

        def _tables(table_name):
            from tilequeue.query.common import Table
            source = self.table_sources[table_name]
            return Table(source, data.get(table_name, []))

        return _tables


def _make_rawr_fetcher(cfg, layer_data, query_cfg, io_pool):
    rawr_yaml = cfg.yml.get('rawr')
    assert rawr_yaml is not None, 'Missing rawr configuration in yaml'

    group_by_zoom = rawr_yaml.get('group-zoom')
    assert group_by_zoom is not None, 'Missing group-zoom rawr config'

    rawr_source_yaml = rawr_yaml.get('source')
    assert rawr_source_yaml, 'Missing rawr source config'

    table_sources = rawr_source_yaml.get('table-sources')
    assert table_sources, 'Missing definitions of source per table'

    # map text for table source onto Source objects
    for tbl, data in table_sources.items():
        source_name = data['name']
        source_value = data['value']
        table_sources[tbl] = Source(source_name, source_value)

    # set this flag, and provide a postgresql subkey, to generate RAWR tiles
    # directly, rather than trying to load them from S3. this can be useful
    # for standalone use and testing.
    is_s3_storage = not rawr_source_yaml.get('generate-from-scratch')

    if is_s3_storage:
        bucket = rawr_source_yaml.get('bucket')
        assert bucket, 'Missing rawr sink bucket'
        prefix = rawr_source_yaml.get('prefix')
        assert prefix, 'Missing rawr sink prefix'
        suffix = rawr_source_yaml.get('suffix')
        assert suffix, 'Missing rawr sink suffix'
        allow_missing_tiles = rawr_source_yaml.get(
            'allow-missing-tiles', False)

        import boto3
        from tilequeue.rawr import RawrS3Source
        s3_client = boto3.client('s3')
        storage = RawrS3Source(s3_client, bucket, prefix, suffix,
                               table_sources, io_pool, allow_missing_tiles)

    else:
        from raw_tiles.source.conn import ConnectionContextManager
        from raw_tiles.source.osm import OsmSource

        postgresql_cfg = rawr_source_yaml.get('postgresql')
        assert postgresql_cfg, 'Missing rawr postgresql config'

        conn_ctx = ConnectionContextManager(postgresql_cfg)
        rawr_osm_source = OsmSource(conn_ctx)
        storage = _NullRawrStorage(rawr_osm_source, table_sources)

    # TODO: this needs to be configurable, everywhere!
    max_z = 16

    # TODO: put this in the config!
    label_placement_layers = {
        'point': set(['earth', 'water']),
        'polygon': set(['buildings', 'earth', 'landuse', 'water']),
        'linestring': set(['earth', 'landuse', 'water']),
    }

    layers = _make_layer_info(layer_data, cfg.process_yaml_cfg)

    return make_rawr_data_fetcher(
        group_by_zoom, max_z, storage, layers, label_placement_layers)


def _make_layer_info(layer_data, process_yaml_cfg):
    from tilequeue.query.common import LayerInfo

    layers = {}
    functions = _parse_yaml_functions(process_yaml_cfg)

    for layer_datum in layer_data:
        name = layer_datum['name']
        min_zoom_fn, props_fn = functions[name]
        shape_types = _parse_shape_types(layer_datum['geometry_types'])
        layer_info = LayerInfo(min_zoom_fn, props_fn, shape_types)
        layers[name] = layer_info

    return layers


def _parse_shape_types(inputs):
    outputs = set()
    for value in inputs:
        if value.startswith('Multi'):
            value = value[len('Multi'):]
        outputs.add(value.lower())

    return outputs or None


def _parse_yaml_functions(process_yaml_cfg):
    from tilequeue.command import make_output_calc_mapping
    from tilequeue.command import make_min_zoom_calc_mapping

    output_layer_data = make_output_calc_mapping(process_yaml_cfg)
    min_zoom_layer_data = make_min_zoom_calc_mapping(process_yaml_cfg)

    keys = set(output_layer_data.keys())
    assert keys == set(min_zoom_layer_data.keys())

    functions = {}
    for key in keys:
        min_zoom_fn = min_zoom_layer_data[key]
        output_fn = output_layer_data[key]
        functions[key] = (min_zoom_fn, output_fn)

    return functions