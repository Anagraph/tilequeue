from collections import namedtuple
from shapely.geometry import box
from tilequeue.process import lookup_source
from itertools import izip
from tilequeue.transform import calculate_padded_bounds


def namedtuple_with_defaults(name, props, defaults):
    t = namedtuple(name, props)
    t.__new__.__defaults__ = defaults
    return t


class LayerInfo(namedtuple_with_defaults(
        'LayerInfo', 'min_zoom_fn props_fn shape_types', (None,))):

    def allows_shape_type(self, shape):
        if self.shape_types is None:
            return True
        typ = _shape_type_lookup(shape)
        return typ in self.shape_types


def deassoc(x):
    """
    Turns an array consisting of alternating key-value pairs into a
    dictionary.

    Osm2pgsql stores the tags for ways and relations in the planet_osm_ways and
    planet_osm_rels tables in this format. Hstore would make more sense now,
    but this encoding pre-dates the common availability of hstore.

    Example:
    >>> from raw_tiles.index.util import deassoc
    >>> deassoc(['a', 1, 'b', 'B', 'c', 3.14])
    {'a': 1, 'c': 3.14, 'b': 'B'}
    """

    pairs = [iter(x)] * 2
    return dict(izip(*pairs))


# fixtures extend metadata to include ways and relations for the feature.
# this is unnecessary for SQL, as the ways and relations tables are
# "ambiently available" and do not need to be passed in arguments.
Metadata = namedtuple('Metadata', 'source ways relations')


def _shape_type_lookup(shape):
    typ = shape.geom_type
    if typ.startswith('Multi'):
        typ = typ[len('Multi'):]
    return typ.lower()


# list of road types which are likely to have buses on them. used to cut
# down the number of queries the SQL used to do for relations. although this
# isn't necessary for fixtures, we replicate the logic to keep the behaviour
# the same.
BUS_ROADS = set([
    'motorway', 'motorway_link', 'trunk', 'trunk_link', 'primary',
    'primary_link', 'secondary', 'secondary_link', 'tertiary',
    'tertiary_link', 'residential', 'unclassified', 'road', 'living_street',
])


class DataFetcher(object):

    def __init__(self, layers, rows, label_placement_layers):
        """
        Expect layers to be a dict of layer name to LayerInfo. Expect rows to
        be a list of (fid, shape, properties). Label placement layers should
        be a set of layer names for which to generate label placement points.
        """

        self.layers = layers
        self.rows = rows
        self.label_placement_layers = label_placement_layers

    def __call__(self, zoom, unpadded_bounds):
        read_rows = []
        bbox = box(*unpadded_bounds)

        for (fid, shape, props) in self.rows:
            # reject any feature which doesn't intersect the given bounds
            if bbox.disjoint(shape):
                continue

            # copy props so that any updates to it don't affect the original
            # data.
            props = props.copy()

            # TODO: there must be some better way of doing this?
            rels = props.pop('__relations__', [])
            ways = props.pop('__ways__', [])

            # place for assembing the read row as if from postgres
            read_row = {}

            # whether to generate a label placement centroid
            generate_label_placement = False

            # whether to clip to a padded box
            has_water_layer = False

            for layer_name, info in self.layers.items():
                if not info.allows_shape_type(shape):
                    continue

                source = lookup_source(props.get('source'))
                meta = Metadata(source, ways, rels)
                min_zoom = info.min_zoom_fn(shape, props, fid, meta)

                # reject features which don't match in this layer
                if min_zoom is None:
                    continue

                # reject anything which isn't in the current zoom range
                # note that this is (zoom+1) because things with a min_zoom of
                # (e.g) 14.999 should still be in the zoom 14 tile.
                #
                # also, if zoom >= 16, we should include all features, even
                # those with min_zoom > zoom.
                if zoom < 16 and (zoom + 1) <= min_zoom:
                    continue

                # UGLY HACK: match the query for "max zoom" for NE places.
                # this removes larger cities at low zooms, and smaller cities
                # as the zoom increases and as the OSM cities start to "fade
                # in".
                if props.get('source') == 'naturalearthdata.com':
                    pop_max = int(props.get('pop_max', '0'))
                    remove = ((zoom >= 8 and zoom < 10 and pop_max > 50000) or
                              (zoom >= 10 and zoom < 11 and pop_max > 20000) or
                              (zoom >= 11 and pop_max > 5000))
                    if remove:
                        continue

                # if the feature exists in any label placement layer, then we
                # should consider generating a centroid
                label_layers = self.label_placement_layers.get(
                    _shape_type_lookup(shape), {})
                if layer_name in label_layers:
                    generate_label_placement = True

                layer_props = props.copy()
                layer_props['min_zoom'] = min_zoom

                # need to make sure that the name is only applied to one of
                # the pois, landuse or buildings layers - in that order of
                # priority.
                #
                # TODO: do this for all name variants & translations
                if layer_name in ('pois', 'landuse', 'buildings'):
                    layer_props.pop('name', None)

                # urgh, hack!
                if layer_name == 'water' and shape.geom_type == 'Point':
                    layer_props['label_placement'] = True

                if shape.geom_type in ('Polygon', 'MultiPolygon'):
                    layer_props['area'] = shape.area

                if layer_name == 'roads' and \
                   shape.geom_type in ('LineString', 'MultiLineString'):
                    mz_networks = []
                    mz_cycling_networks = set()
                    mz_is_bus_route = False
                    for rel in rels:
                        rel_tags = deassoc(rel['tags'])
                        typ, route, network, ref = [rel_tags.get(k) for k in (
                            'type', 'route', 'network', 'ref')]
                        if route and (network or ref):
                            mz_networks.extend([route, network, ref])
                        if typ == 'route' and \
                           route in ('hiking', 'foot', 'bicycle') and \
                           network in ('icn', 'ncn', 'rcn', 'lcn'):
                            mz_cycling_networks.add(network)
                        if typ == 'route' and route in ('bus', 'trolleybus'):
                            mz_is_bus_route = True

                    mz_cycling_network = None
                    for cn in ('icn', 'ncn', 'rcn', 'lcn'):
                        if layer_props.get(cn) == 'yes' or \
                           ('%s_ref' % cn) in layer_props or \
                           cn in mz_cycling_networks:
                            mz_cycling_network = cn
                            break

                    if mz_is_bus_route and \
                       zoom >= 12 and \
                       layer_props.get('highway') in BUS_ROADS:
                        layer_props['is_bus_route'] = True

                    layer_props['mz_networks'] = mz_networks
                    if mz_cycling_network:
                        layer_props['mz_cycling_network'] = mz_cycling_network

                if layer_props:
                    props_name = '__%s_properties__' % layer_name
                    read_row[props_name] = layer_props
                    if layer_name == 'water':
                        has_water_layer = True

            # if at least one min_zoom / properties match
            if read_row:
                clip_box = bbox
                if has_water_layer:
                    pad_factor = 1.1
                    clip_box = calculate_padded_bounds(
                        pad_factor, unpadded_bounds)
                clip_shape = clip_box.intersection(shape)

                # add back name into whichever of the pois, landuse or
                # buildings layers has claimed this feature.
                name = props.get('name', None)
                if name:
                    for layer_name in ('pois', 'landuse', 'buildings'):
                        props_name = '__%s_properties__' % layer_name
                        if props_name in read_row:
                            read_row[props_name]['name'] = name
                            break

                read_row['__id__'] = fid
                read_row['__geometry__'] = bytes(clip_shape.wkb)
                if generate_label_placement:
                    read_row['__label__'] = bytes(
                        shape.representative_point().wkb)
                read_rows.append(read_row)

        return read_rows


def make_fixture_data_fetcher(layers, rows, label_placement_layers={}):
    return DataFetcher(layers, rows, label_placement_layers)
