# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import json
import unicodecsv as csv
import mapbox_vector_tile
from collections import OrderedDict
from datetime import date, time, datetime
from StringIO import StringIO

import geojson
from math import pi
from shapely import wkt
from pyramid.response import Response

from ..resource import DataScope, resource_factory
from ..models import DBSession
from ..vector_layer import VectorLayer

from .interface import IFeatureLayer, IWritableFeatureLayer, FIELD_TYPE
from .feature import Feature
from .extension import FeatureExtension


PERM_READ = DataScope.read
PERM_WRITE = DataScope.write


class ComplexEncoder(geojson.GeoJSONEncoder):
    def default(self, obj):
        try:
            return geojson.GeoJSONEncoder.default(self, obj)
        except TypeError:
            return str(obj)


def view_geojson(request):
    request.resource_permission(PERM_READ)

    class CRSProxy(object):
        """ Класс обертка добавляющая информацию о системе координат в
        геоинтерфейс результата запроса векторного слоя """

        def __init__(self, query):
            self.query = query

        @property
        def __geo_interface__(self):
            result = self.query.__geo_interface__

            # TODO: Нужен корректный способ генерации имени СК, пока по ID
            result['crs'] = dict(type='name', properties=dict(
                name='EPSG:%d' % request.context.srs_id))
            return result

    query = request.context.feature_query()
    query.geom()

    content_disposition = (b'attachment; filename=%d.geojson'
                           % request.context.id)

    result = CRSProxy(query())

    return Response(
        geojson.dumps(result, ensure_ascii=False, cls=ComplexEncoder),
        content_type=b'application/json',
        content_disposition=content_disposition)


def view_csv(request):
    request.resource_permission(PERM_READ)

    buf = StringIO()
    writer = csv.writer(buf, dialect='excel')

    headrow = map(lambda fld: fld.keyname, request.context.fields)
    headrow.append('GEOM')
    writer.writerow(headrow)

    query = request.context.feature_query()
    query.geom()

    for feature in query():
        datarow = map(
            lambda fld: feature.fields[fld.keyname],
            request.context.fields)
        datarow.append(feature.geom.wkt)
        writer.writerow(datarow)

    content_disposition = (b'attachment; filename=%d.csv'
                           % request.context.id)

    return Response(
        buf.getvalue(), content_type=b'text/csv',
        content_disposition=content_disposition)


def view_mvt(request):
    # Эксперимент с Mapbox Vector Tile.
    # Используется функци PostGIS ST_ClipByBox2D, доступная в версии 2.2.
    # http://javisantana.com/2015/03/22/vector-tiles.html
    # https://github.com/mapzen/mapbox-vector-tile
    request.resource_permission(PERM_READ)
    resource = request.context

    # TODO: Добавить перепроецирование в 3857
    assert resource.srs_id == 3857
    assert isinstance(resource, VectorLayer)

    mvt_extent = 4096
    mvt_layer = dict(name=str(resource.id), features=[])

    tile_zxy = (int(request.matchdict['z']),
                int(request.matchdict['x']),
                int(request.matchdict['y']))

    tile_extent = resource.srs.tile_extent(tile_zxy)
    minx, miny, maxx, maxy = tile_extent

    # Увеличиваем охват запрашиваемой области на 5% с каждой стороны,
    # чтобы избежать возможных артефактов на границах тайлов,
    # также это позволит корректно отображать точечные данные
    tile_extent_padded = (minx - (maxx - minx)*0.05,
                          miny - (maxy - miny)*0.05,
                          maxx + (maxx - minx)*0.05,
                          maxy + (maxy - miny)*0.05)
    minxp, minyp, maxxp, maxyp = tile_extent_padded

    # Параметры аффинного преобразования
    dx = -minx
    dy = -miny
    resx = mvt_extent / (maxx - minx)
    resy = mvt_extent / (maxy - miny)

    props = [('id', 'id')]
    if request.env.feature_layer.settings.get('mvt.attributes'):
        props += [('fld_%s' % f.fld_uuid, f.keyname) for f in resource.fields]

    props_sql = ','.join(['%s AS "%s"' % (fld, label) for (fld, label) in props])
    props_fld = ','.join(['"%s"' % prop[1] for prop in props])
    keynames = [prop[1] for prop in props]

    resolutions = [6378137 * 2 * pi / (2 ** (z + 8)) for z in range(20)]

    # TODO: ST_SnapToGrid?
    # TODO: ST_Simplify (определить величину упрощения)
    query = """
        WITH _geom AS (
            SELECT %(props_sql)s,
                   ST_ClipByBox2d(
                       ST_Simplify(
                           geom,
                           %(resolution)f/2
                       ),
                       ST_MakeEnvelope(%(minxp)f, %(minyp)f,
                                       %(maxxp)f, %(maxyp)f)
                   ) AS _clip_geom
            FROM vector_layer.%(tbl_uuid)s
            WHERE geom && ST_MakeEnvelope(%(minxp)f, %(minyp)f,
                                          %(maxxp)f, %(maxyp)f)
        )
        SELECT %(props_fld)s, ST_AsBinary(
            ST_Affine(_clip_geom, %(resx)f, 0, 0, %(resy)f,
                      %(resx)f*%(dx)f, %(resy)f*%(dy)f)
        ) AS geom
        FROM _geom
        WHERE NOT ST_IsEmpty(_clip_geom)
    """ % dict(dx=dx, dy=dy, resx=resx, resy=resy,
               minxp=minxp, minyp=minyp, maxxp=maxxp, maxyp=maxyp,
               resolution=resolutions[tile_zxy[0]],
               tbl_uuid=request.context._tablename,
               props_sql=props_sql, props_fld=props_fld)

    rows = DBSession.connection().execute(query)
    for feature in rows:
        properties = dict()
        geom = str(feature['geom'])
        for key in keynames:
            properties[key] = feature[key]
        mvt_layer['features'].append(dict(geometry=geom,
                                          properties=properties))

    return Response(
        mapbox_vector_tile.encode([mvt_layer]),
        content_type=b'application/vnd.mapbox-vector-tile')


def deserialize(feat, data):
    if 'geom' in data:
        feat.geom = data['geom']

    if 'fields' in data:
        fdata = data['fields']

        for fld in feat.layer.fields:

            if fld.keyname in fdata:
                val = fdata.get(fld.keyname)

                if val is None:
                    fval = None

                elif fld.datatype == FIELD_TYPE.DATE:
                    fval = date(
                        int(val['year']),
                        int(val['month']),
                        int(val['day']))

                elif fld.datatype == FIELD_TYPE.TIME:
                    fval = time(
                        int(val['hour']),
                        int(val['minute']),
                        int(val['second']))

                elif fld.datatype == FIELD_TYPE.DATETIME:
                    fval = datetime(
                        int(val['year']),
                        int(val['month']),
                        int(val['day']),
                        int(val['hour']),
                        int(val['minute']),
                        int(val['second']))

                else:
                    fval = val

                feat.fields[fld.keyname] = fval

    if 'extensions' in data:
        for cls in FeatureExtension.registry:
            if cls.identity in data['extensions']:
                ext = cls(feat.layer)
                ext.deserialize(feat, data['extensions'][cls.identity])


def serialize(feat):
    result = OrderedDict(id=feat.id)
    result['geom'] = wkt.dumps(feat.geom)

    result['fields'] = OrderedDict()
    for fld in feat.layer.fields:

        val = feat.fields.get(fld.keyname)

        if val is None:
            fval = None

        elif fld.datatype == FIELD_TYPE.DATE:
            fval = OrderedDict((
                ('year', val.year),
                ('month', val.month),
                ('day', val.day)))

        elif fld.datatype == FIELD_TYPE.TIME:
            fval = OrderedDict((
                ('hour', val.hour),
                ('minute', val.minute),
                ('second', val.second)))

        elif fld.datatype == FIELD_TYPE.DATETIME:
            fval = OrderedDict((
                ('year', val.year),
                ('month', val.month),
                ('day', val.day),
                ('hour', val.hour),
                ('minute', val.minute),
                ('second', val.second)))

        else:
            fval = val

        result['fields'][fld.keyname] = fval

    result['extensions'] = OrderedDict()
    for cls in FeatureExtension.registry:
        ext = cls(feat.layer)
        result['extensions'][cls.identity] = ext.serialize(feat)

    return result


def iget(resource, request):
    request.resource_permission(PERM_READ)

    query = resource.feature_query()
    query.geom()

    query.filter_by(id=request.matchdict['fid'])
    query.limit(1)

    result = None
    for f in query():
        result = f

    return Response(
        json.dumps(serialize(result)),
        content_type=b'application/json')


def iput(resource, request):
    request.resource_permission(PERM_WRITE)

    query = resource.feature_query()
    query.geom()

    query.filter_by(id=request.matchdict['fid'])
    query.limit(1)

    feature = None
    for f in query():
        feature = f

    deserialize(feature, request.json_body)
    if IWritableFeatureLayer.providedBy(resource):
        resource.feature_put(feature)

    return Response(
        json.dumps(dict(id=feature.id)),
        content_type=b'application/json')


def idelete(resource, request):
    request.resource_permission(PERM_WRITE)

    fid = int(request.matchdict['fid'])
    resource.feature_delete(fid)

    return Response(json.dumps(None), content_type=b'application/json')


def cget(resource, request):
    request.resource_permission(PERM_READ)

    query = resource.feature_query()
    query.geom()

    result = map(serialize, query())

    return Response(
        json.dumps(result),
        content_type=b'application/json')


def cpost(resource, request):
    request.resource_permission(PERM_WRITE)

    feature = Feature(layer=resource)
    deserialize(feature, request.json_body)
    fid = resource.feature_create(feature)

    return Response(
        json.dumps(dict(id=fid)),
        content_type=b'application/json')


def cdelete(resource, request):
    request.resource_permission(PERM_WRITE)
    resource.feature_delete_all()

    return Response(json.dumps(None), content_type=b'application/json')


def count(resource, request):
    request.resource_permission(PERM_READ)

    query = resource.feature_query()
    total_count = query().total_count

    return Response(
        json.dumps(dict(total_count=total_count)),
        content_type=b'application/json')


def setup_pyramid(comp, config):
    config.add_route(
        'feature_layer.geojson', '/api/resource/{id}/geojson',
        factory=resource_factory) \
        .add_view(view_geojson, context=IFeatureLayer, request_method='GET')

    config.add_route(
        'feature_layer.csv', '/api/resource/{id}/csv',
        factory=resource_factory) \
        .add_view(view_csv, context=IFeatureLayer, request_method='GET')

    config.add_route(
        'feature_layer.mvt', '/api/resource/{id}/{z:\d+}/{x:\d+}/{y:\d+}.mvt',
        factory=resource_factory) \
        .add_view(view_mvt, context=IFeatureLayer, request_method='GET',
                  http_cache=3600)

    config.add_route(
        'feature_layer.feature.item', '/api/resource/{id}/feature/{fid}',
        factory=resource_factory) \
        .add_view(iget, context=IFeatureLayer, request_method='GET') \
        .add_view(iput, context=IFeatureLayer, request_method='PUT') \
        .add_view(idelete, context=IWritableFeatureLayer,
                  request_method='DELETE')

    config.add_route(
        'feature_layer.feature.collection', '/api/resource/{id}/feature/',
        factory=resource_factory) \
        .add_view(cget, context=IFeatureLayer, request_method='GET') \
        .add_view(cpost, context=IWritableFeatureLayer, request_method='POST') \
        .add_view(cdelete, context=IWritableFeatureLayer,
                  request_method='DELETE')

    config.add_route(
        'feature_layer.feature.count', '/api/resource/{id}/feature_count',
        factory=resource_factory) \
        .add_view(count, context=IFeatureLayer, request_method='GET')

    # Legacy route
    config.add_route(
        '#feature_layer.geojson', '/resource/{id:\d+}/geojson/',
        factory=resource_factory) \
        .add_view(view_geojson, context=IFeatureLayer, request_method='GET')
