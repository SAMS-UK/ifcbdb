import re
import json
import logging
import os

from functools import lru_cache

from django.db import models

from django.conf import settings

from django.db.models import F, Count, Sum, Avg, Min, Max
from django.db.models.functions import Trunc
from django.contrib.auth.models import User
from django.contrib.gis.db.models import PointField
from django.contrib.gis.geos import Point, Polygon
from django.contrib.gis.db.models.functions import Distance

from django.db.models.signals import pre_save
from django.dispatch import receiver

from django.core.cache import cache

import pandas as pd

import ifcb

from ifcb.data.adc import SCHEMA_VERSION_1
from ifcb.data.stitching import InfilledImages
from ifcb.viz.mosaic import Mosaic
from ifcb.viz.blobs import blob_outline
from ifcb.data.adc import schema_names
from ifcb.data.products.blobs import BlobDirectory
from ifcb.data.products.features import FeaturesDirectory
from ifcb.data.products.class_scores import ClassScoresDirectory
from ifcb.data.zip import bin2zip_stream
from ifcb.data.transfer import RemoteIfcb
from ifcb.data.files import Fileset, FilesetBin

from .crypto import AESCipher
from .tasks import mosaic_coordinates_task

logger = logging.getLogger(__name__)

FILL_VALUE = -9999999
SRID = 4326

def do_nothing(*args, **kw):
    pass

class Timeline(object):

    TIMELINE_METRICS = {
        "size": "Bytes",
        "temperature": "Degrees C",
        "humidity": "Percentage",
        "run_time": "Seconds",
        "look_time": "Seconds",
        "ml_analyzed": "Milliliters",
        'concentration': 'ROIs / ml',
        'n_triggers': 'Count',
        'n_images': 'Count',
    }

    def __init__(self, bin_qs, filter_skip=True):
        self.bins = bin_qs
        if filter_skip:
            self.bins = self.bins.filter(skip=False)

    def time_range(self, start_time=None, end_time=None):
        qs = self.bins
        if start_time is not None:
            ts = pd.to_datetime(start_time, utc=True)
            qs = qs.filter(sample_time__gte=ts)
        if end_time is not None:
            ts = pd.to_datetime(end_time, utc=True)
            qs = qs.filter(sample_time__lte=ts)
        return qs

    def most_recent_bin(self, time=None):
        b = self.time_range(end_time=time).order_by('-sample_time').first()
        if b is None:
            # time is before first bin
            return self.bins.order_by('sample_time').first()
        else:
            return b

    def bin_closest_in_time(self, time=None):
        if time is None:
            return self.most_recent_bin()

        previous_bin = self.time_range(end_time=time).order_by('-sample_time').first()
        next_bin = self.time_range(start_time=time).order_by('sample_time').first()

        if previous_bin is None:
            return next_bin
        if next_bin is None:
            return previous_bin

        time_to_next = next_bin.timestamp - time
        time_to_prev = time - previous_bin.timestamp

        if time_to_next < time_to_prev:
            return next_bin
        else:
            return previous_bin

    def previous_bin(self, bin):
        return self.bins.filter(sample_time__lt=bin.sample_time).order_by("-sample_time").first()

    def next_bin(self, bin):
        return self.bins.filter(sample_time__gt=bin.sample_time).order_by("sample_time").first()

    def nearest_bin(self, longitude, latitude):
        location = Point(longitude, latitude, srid=SRID)
        return self.bins.annotate(
            distance=Distance('location', location)
        ).order_by('distance').first()

    def metrics(self, metric, start_time=None, end_time=None, resolution='day', apply_offset=True):
        if resolution not in ['month', 'week', 'day', 'hour', 'bin', 'auto']:
            raise ValueError('unsupported time resolution {}'.format(resolution))

        if metric not in self.TIMELINE_METRICS.keys():
            raise ValueError('unsupported metric {}'.format(metric))

        if resolution == 'auto':
            if start_time is None or end_time is None:
                mm = self.bins.aggregate(min=Min('sample_time'),max=Max('sample_time'))
                min_sample_time, max_sample_time = mm['min'], mm['max']
            if start_time is None:
                start_time = min_sample_time
            else:
                start_time = pd.to_datetime(start_time, utc=True)
            if end_time is None:
                end_time = max_sample_time
            else:
                end_time = pd.to_datetime(end_time, utc=True)
            time_range = end_time - start_time
            if time_range < pd.Timedelta('7d'):
                resolution = 'bin'
            elif time_range < pd.Timedelta('60d'):
                resolution = 'hour'
            elif time_range < pd.Timedelta('3y'):
                resolution = 'day'
            else:
                resolution = 'week'

        if apply_offset:
            if resolution == 'bin':
                offset = pd.Timedelta('0s')
            elif resolution == 'hour':
                offset = pd.Timedelta('30m')
            elif resolution == 'day':
                offset = pd.Timedelta('12h')
            elif resolution == 'week':
                offset = pd.Timedelta('3.5d')
                
        qs = self.time_range(start_time, end_time)

        aggregate_fn = Avg

        if resolution == 'bin':
            result = qs.annotate(dt=F('sample_time'),metric=F(metric)).values('dt','metric').order_by('dt')
        else:
            result = qs.annotate(dt=Trunc('sample_time', resolution)). \
                    values('dt').annotate(metric=aggregate_fn(metric)).order_by('dt')

            if apply_offset:
                for record in result:
                    record['dt'] += offset

        return result, resolution

    @classmethod
    def metric_label(cls, metric):
        return cls.TIMELINE_METRICS.get(metric,'')

    def __len__(self):
        return self.bins.count()

    def n_images(self):
        return self.bins.aggregate(Sum('n_images'))['n_images__sum']

    def total_data_volume(self):
        # total data size in bytes for everything in this Timeline
        return self.bins.aggregate(Sum('size'))['size__sum']       

def bin_query(dataset_name=None, start=None, end=None, tags=[], instrument_number=None, filter_skip=True):
    qs = Bin.objects
    if filter_skip:
        qs = qs.filter(skip=False)
    if start is not None or end is not None:
        qs = Timeline(qs).time_range(start, end)
    if dataset_name:
        qs = qs.filter(datasets__name=dataset_name)
    if tags is not None:
        for tag in tags:
            qs = qs.filter(tags__name__iexact=tag)
    if instrument_number is not None:
        qs = qs.filter(instrument__number=instrument_number)
    return qs

class Dataset(models.Model):
    name = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=256)
    is_active = models.BooleanField(blank=False, null=False, default=True)

    # for fixed deployment
    location = PointField(null=True)
    depth = models.FloatField(default=0)

    def __len__(self):
        # number of bins
        return self.bins.count()

    def data_volume(self):
        # total data volume in bytes
        return Timeline(self.bins).total_data_volume()

    def tag_cloud(self, instrument=None):
        return Tag.cloud(dataset=self, instrument=instrument)

    @staticmethod
    def in_bounding_box(sw_lon, sw_lat, ne_lon, ne_lat):
        # return the ids of all datasets in the bounding box
        bbox = Polygon.from_bbox((sw_lon, sw_lat, ne_lon, ne_lat))
        ds = Bin.objects.filter(location__contained=bbox).values('datasets').distinct()
        return ds

    @staticmethod
    def search(start_date=None, end_date=None, min_depth=None, max_depth=None, region=None):
        # TODO: Check into optimizing query
        datasets = Dataset.objects.filter(is_active=True).prefetch_related("bins")

        # Handle start/end dates
        if start_date and end_date:
            datasets = datasets.filter(bins__timestamp__range=[start_date, end_date])
        elif start_date:
            datasets = datasets.filter(bins__timestamp__gte=start_date)
        elif end_date:
            datasets = datasets.filter(bins__timestamp__lt=end_date)

        # Handle min/max depth
        if min_depth and max_depth:
            datasets = datasets.filter(bins__depth__range=[min_depth, max_depth])
        elif min_depth:
            datasets = datasets.filter(bins__depth__gte=min_depth)
        elif max_depth:
            datasets = datasets.filter(bins__depth__lte=max_depth)

        # Handle region; requires an array of sw_lon, sw_lat, ne_lon, ne_lat
        if region:
            bbox = Polygon.from_bbox(region)
            datasets = datasets.filter(bins__location__contained=bbox)

        return datasets.order_by("title").distinct("title")

    def set_location(self, longitude, latitude, depth=None):
        # convenience function for setting location w/o having to construct Point object
        self.location = Point(longitude, latitude, srid=SRID)
        if depth is not None:
            self.depth = depth

    @property
    def latitude(self):
        if self.location is None:
            return FILL_VALUE
        return self.location.y

    @property
    def longitude(self):
        if self.location is None:
            return FILL_VALUE
        return self.location.x

    def __str__(self):
        return self.name

class DataDirectory(models.Model):
    # directory types
    RAW = 'raw'
    BLOBS = 'blobs'
    FEATURES = 'features'
    CLASS_SCORES = 'class_scores'

    DEFAULT_VERSION = 2

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name='directories')
    path = models.CharField(max_length=512) # absolute path
    kind = models.CharField(max_length=32, default=RAW)
    priority = models.IntegerField(default=1) # order in which directories are searched (lower ealier)
    last_synced = models.DateTimeField('time of last db sync', blank=True, null=True)
    # parameters controlling searching (simple comma separated fields because we don't have to query on these)
    whitelist = models.CharField(max_length=512, default='data') # comma separated list of directory names to search
    blacklist = models.CharField(max_length=512, default='skip,bad') # comma separated list of directory names to skip
    # for product directories, the product version
    version = models.IntegerField(null=True, blank=True)

    def get_raw_directory(self):
        if self.kind != self.RAW:
            raise ValueError('not a raw directory')
        # return the underlying ifcb.DataDirectory
        whitelist = re.split(',', self.whitelist)
        blacklist = re.split(',', self.blacklist)
        return ifcb.DataDirectory(self.path, whitelist=whitelist, blacklist=blacklist)

    def raw_destination(self, bin_id):
        # where to put an incoming bin with the given id
        return self.path # FIXME support year/day directories

    def get_blob_directory(self):
        if self.kind != self.BLOBS:
            raise ValueError('not a blobs directory')
        return BlobDirectory(self.path, self.version)

    def get_features_directory(self):
        if self.kind != self.FEATURES:
            raise ValueError('not a features directory')
        return FeaturesDirectory(self.path, self.version)

    def get_class_scores_directory(self):
        if self.kind != self.CLASS_SCORES:
            raise ValueError('not a class scores directory')
        return ClassScoresDirectory(self.path, self.version)

    def __str__(self):
        return '{} ({})'.format(self.path, self.kind)

class Bin(models.Model):
    # bin's permanent identifier (e.g., D20190102T1234_IFCB927)
    pid = models.CharField(max_length=64, unique=True)
    # the parsed bin timestamp
    timestamp = models.DateTimeField('bin timestamp', db_index=True)
    # spatiotemporal information
    sample_time = models.DateTimeField('sample time', db_index=True)
    location = PointField(null=True)
    depth = models.FloatField(default=0)
    # instrument
    instrument = models.ForeignKey('Instrument', related_name='bins', null=True, on_delete=models.SET_NULL)
    # many-to-many relationship with datasets
    datasets = models.ManyToManyField('Dataset', related_name='bins')
    # accession
    added = models.DateTimeField(auto_now_add=True, null=True)
    # qaqc flags
    qc_bad = models.BooleanField(default=False) # is this bin invalid
    qc_no_rois = models.BooleanField(default=False)
    skip = models.BooleanField(default=False) # user wants to ignore this file
    # metadata JSON
    metadata_json = models.CharField(max_length=8192, default='{}', db_column='metadata')
    # metrics
    size = models.BigIntegerField(default=0) # size of raw data in bytes
    n_triggers = models.IntegerField(default=0)
    n_images = models.IntegerField(default=0)
    temperature = models.FloatField(default=FILL_VALUE)
    humidity = models.FloatField(default=FILL_VALUE)
    run_time = models.FloatField(default=FILL_VALUE)
    look_time = models.FloatField(default=FILL_VALUE)
    ml_analyzed = models.FloatField(default=FILL_VALUE)
    concentration = models.FloatField(default=FILL_VALUE)

    # tags
    tags = models.ManyToManyField('Tag', through='TagEvent')

    MOSAIC_SCALE_FACTORS = [25, 33, 66, 100]
    MOSAIC_VIEW_SIZES = ["640x480", "800x600", "800x1280", "1080x1920"]
    MOSAIC_DEFAULT_SCALE_FACTOR = 33
    MOSAIC_DEFAULT_VIEW_SIZE = "800x600"

    def set_location(self, longitude, latitude, depth=None):
        # convenience function for setting location w/o having to construct Point object
        self.location = Point(longitude, latitude, srid=SRID)
        if depth is not None:
            self.depth = depth

    @property
    def latitude(self):
        if self.location is None:
            return FILL_VALUE
        return self.location.y

    @property
    def longitude(self):
        if self.location is None:
            return FILL_VALUE
        return self.location.x

    @property
    def trigger_frequency(self):
        if self.run_time == 0:
            return 0

        return self.n_triggers / self.run_time
    
    @property
    def metadata(self):
        return json.loads(self.metadata_json)
    
    def set_ml_analyzed(self, ml_analyzed):
        self.ml_analyzed = ml_analyzed
        self.concentration = self.n_images / ml_analyzed

    # access to underlying FilesetBin objects

    def _directories(self, kind=DataDirectory.RAW, version=None):
        for dataset in self.datasets.all():
            qs = dataset.directories.filter(kind=kind)
            if version is not None:
                qs = qs.filter(version=version)
            for directory in qs.order_by('priority'):
                yield directory

    def _get_bin(self):
        cache_key = '{}_path'.format(self.pid)
        cached_path = cache.get(cache_key)
        if cached_path is not None and os.path.exists(cached_path):
            return FilesetBin(Fileset(cached_path))
        # return the underlying ifcb.Bin object backed by the raw filesets
        for directory in self._directories(kind=DataDirectory.RAW):
            dd = directory.get_raw_directory()
            try:
                b = dd[self.pid]
                basepath, _  = os.path.splitext(b.fileset.adc_path)
                cache.set(cache_key, basepath)
                return b
            except KeyError:
                pass # keep searching
        raise KeyError('cannot find fileset for {}'.format(self))

    # access to raw files

    def adc_path(self):
        return self._get_bin().fileset.adc_path

    def hdr_path(self):
        return self._get_bin().fileset.hdr_path

    def roi_path(self):
        return self._get_bin().fileset.roi_path

    # access to images

    def images(self, bin=None):
        if bin is None:
            b = self._get_bin()
        else:
            b = bin
        if b.schema == SCHEMA_VERSION_1:
            return InfilledImages(b)
        else:
            return b.images

    def image(self, target_number):
        b = self._get_bin()
        with b.as_single(target_number) as subset:
            ii = self.images(subset)
            try:
                return ii[target_number]
            except IndexError as e:
                raise KeyError('no such image {} {}'.format(self.pid, target_number)) from e
            except pd.errors.EmptyDataError as e:
                raise KeyError('no such image {} {}'.format(self.pid, target_number)) from e

    def list_images(self):
        return list(self.images().keys())

    # access to blobs

    def blob_file(self, version=None):
        for directory in self._directories(kind=DataDirectory.BLOBS, version=version):
            bd = directory.get_blob_directory()
            try:
                return bd[self.pid]
            except KeyError as e:
                pass
        raise KeyError('no blobs found for {}'.format(self.pid))

    def has_blobs(self, version=None):
        try:
            self.blob_file(version=version)
            return True
        except KeyError:
            return False

    def blob_path(self, version=None):
        return self.blob_file(version=version).path

    def blob(self, target_number, version=None):
        try:
            bf = self.blob_file(version=version)
            return bf[target_number]
        except KeyError as e:
            raise KeyError('no such blob {} {}'.format(self.pid, target_number)) from e

    def outline(self, target_number, blob_version=None, outline_color=[255, 0, 0]):
        image = self.image(target_number)
        blob = self.blob(target_number, version=blob_version)
        out = blob_outline(image, blob, outline_color=outline_color)
        return out

    # features

    def features_file(self, version=None):
        for directory in self._directories(kind=DataDirectory.FEATURES, version=version):
            fd = directory.get_features_directory()
            try:
                return fd[self.pid]
            except KeyError:
                pass
        raise KeyError('no features found for {}'.format(self.pid))

    def has_features(self, version=None):
        try:
            self.features_file(version=version)
            return True
        except KeyError:
            return False

    def features_path(self, version=None):
        return self.features_file(version=version).path

    def features(self, version=None):
        return self.features_file(version=version).features(prune=True)

    # class scores

    def class_scores_file(self, version=None):
        for directory in self._directories(kind=DataDirectory.CLASS_SCORES, version=version):
            csd = directory.get_class_scores_directory()
            try:
                return csd[self.pid]
            except KeyError:
                pass
        raise KeyError('no class scores found for {}'.format(self.pid))

    def has_class_scores(self, version=None):
        try:
            self.class_scores_file(version=version)
            return True
        except KeyError:
            return False

    def class_scores_path(self, version=None):
        return self.class_scores_file(version=version).path

    def class_scores(self, version=1):
        return self.class_scores_file(version=version).class_scores()

    # mosaics

    def mosaic_coordinates(self, shape=(600,800), scale=0.33, block=True):
        h, w = shape
        cache_key = 'mosaic_coords_{}_{}x{}_{}'.format(self.pid, h, w, int(scale*100))
        cached = cache.get(cache_key)
        if cached is not None:
            return pd.DataFrame.from_dict(cached)
        task = mosaic_coordinates_task.delay(self.pid, shape, scale, cache_key)
        if block:
            try:
                d = task.get()
                return pd.DataFrame.from_dict(d)
            except:
                return pd.DataFrame()
        return None

    def mosaic(self, page=0, shape=(600,800), scale=0.33, bg_color=200):
        b = self._get_bin().read()
        coordinates = self.mosaic_coordinates(shape, scale)
        m = Mosaic(b, shape, scale=scale, bg_color=bg_color, coordinates=coordinates)
        image = m.page(page)
        return image, coordinates        

    def target_id(self, target_number):
        return ifcb.Pid(self.pid).with_target(target_number)

    def target_metadata(self, target_number):
        b = self._get_bin()
        try:
            raw_metadata = b[target_number]
        except KeyError:
            return {}
        names = schema_names(b.schema)
        metadata = dict(zip(names, raw_metadata))
        if self.has_features():
            df = self.features()
            try:
                target_features = df.loc[target_number].to_dict()
                metadata.update(target_features)
            except KeyError:
                pass
        return metadata

    # zip file
    def zip(self):
        return bin2zip_stream(self._get_bin())

    # tags

    @property
    def tag_names(self):
        return [t.name for t in self.tags.all()]

    def add_tag(self, tag_name, user=None):
        tag, created = Tag.objects.get_or_create(name=tag_name)
        # don't add this tag if was already added
        event, created = TagEvent.objects.get_or_create(bin=self, tag=tag, user=user)
        return event

    def delete_tag(self, tag_name):
        tag = Tag.objects.get(name=tag_name)
        event = TagEvent.objects.get(bin=self, tag=tag)
        event.delete()

    # comments

    def add_comment(self, content, user=None):
        comment = Comment(bin=self, content=content, user=user)
        comment.save()

    def delete_comment(self, comment_id, user):
        try:
            comment = Comment.objects.get(bin=self, pk=comment_id)
            if comment.user == user:
                comment.delete()
        except:
            pass

    @property
    def comment_list(self):
        return list(
            self.comments.all()
                .select_related('user')
                .values_list("timestamp", "content", "user__username", "id", "user_id")
                .order_by("-timestamp")
        )
        
    def __str__(self):
        return self.pid


class Instrument(models.Model):
    number = models.IntegerField(unique=True)
    version = models.IntegerField(default=2)
    # nickname is optional, not everyone names their IFCB
    nickname = models.CharField(max_length=64, blank=True)
    # connection parameters for Samba
    address = models.CharField(max_length=128, blank=True) # ip address or dns name
    username = models.CharField(max_length=64, blank=True)
    _password = models.CharField(max_length=128, db_column='password', blank=True)
    share_name = models.CharField(max_length=128, default='Data', blank=True)
    timeout = models.IntegerField(default=30)

    @staticmethod
    def _get_cipher():
        return AESCipher(settings.IFCB_PASSWORD_KEY)

    def set_password(self, password):
        cipher = self._get_cipher()
        self._password = cipher.encrypt(password)

    def get_password(self):
        cipher = self._get_cipher()
        ciphertext = self._password
        if not ciphertext:
            return None
        return cipher.decrypt(ciphertext)

    password = property(get_password, set_password)

    def tag_cloud(self, dataset=None):
        return Tag.cloud(instrument=self, dataset=dataset)

    def __str__(self):
        return self.name

    @staticmethod
    def determine_version(number):
        return 1 if number < 10 else 2

    @property
    def name(self):
        return 'IFCB{}'.format(self.number)
    
    # live instrument access

    def _get_remote(self):
        return RemoteIfcb(self.address, self.username, self.password,
            share=self.share_name, timeout=self.timeout)

    def is_responding(self):
        ifcb = self._get_remote()
        return ifcb.is_responding()

    def list_shares(self):
        with self._get_remote() as ifcb:
            return list(ifcb.list_shares())

    def share_exists(self):
        with self._get_remote() as ifcb:
            return ifcb.share_exists()

    def sync(self, data_directory, progress_callback=do_nothing):
        if not data_directory.kind == DataDirectory.RAW:
            raise TypeError('cannot sync raw data to product directory {}'.format(data_directory))
        def destination_directory(lid):
            return data_directory.raw_destination(lid)
        with self._get_remote() as ifcb:
            ifcb.sync(destination_directory, progress_callback=progress_callback)

# tags

class Tag(models.Model):
    name = models.CharField(max_length=128)

    @staticmethod
    def autocomplete(search_string):
        return Tag.objects.filter(name__istartswith=search_string)

    # Timeline()
    @staticmethod
    def cloud(dataset=None, instrument=None):
        qs = TagEvent.query(dataset, instrument)
        return qs.values('tag').annotate(count=Count('tag')).values('tag__name','count')

    @staticmethod
    def list(dataset=None, instrument=None):
        qs = TagEvent.query(dataset, instrument)
        return [t['tag__name'] for t in qs.values('tag__name').distinct()]

    def __str__(self):
        return self.name

class TagEvent(models.Model):
    bin = models.ForeignKey(Bin, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    timestamp = models.DateTimeField(auto_now_add=True, null=True)

    @staticmethod
    def query(dataset=None, instrument=None):
        qs = TagEvent.objects
        if dataset is not None:
            qs = qs.filter(bin__datasets=dataset)
        if instrument is not None:
            qs = qs.filter(bin__instrument=instrument)
        return qs

    def __str__(self):
        return '{} tagged {}'.format(self.bin, self.tag)

# comments

class Comment(models.Model):
    bin = models.ForeignKey(Bin, on_delete=models.CASCADE, related_name='comments')
    content = models.CharField(max_length=8192)
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        max_length = 20
        if len(self.content) > max_length:
            return self.content[:max_length] + '...'
        else:
            return self.content
