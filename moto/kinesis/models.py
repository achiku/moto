from __future__ import unicode_literals

import datetime
import time

import boto.kinesis
from moto.compat import OrderedDict
from moto.core import BaseBackend
from .exceptions import StreamNotFoundError, ShardNotFoundError, ResourceInUseError
from .utils import compose_shard_iterator, compose_new_shard_iterator, decompose_shard_iterator


class Record(object):
    def __init__(self, partition_key, data, sequence_number):
        self.partition_key = partition_key
        self.data = data
        self.sequence_number = sequence_number

    def to_json(self):
        return {
            "Data": self.data,
            "PartitionKey": self.partition_key,
            "SequenceNumber": str(self.sequence_number),
        }


class Shard(object):
    def __init__(self, shard_id):
        self.shard_id = shard_id
        self.records = OrderedDict()

    def get_records(self, last_sequence_id, limit):
        last_sequence_id = int(last_sequence_id)
        results = []

        for sequence_number, record in self.records.items():
            if sequence_number > last_sequence_id:
                results.append(record)
                last_sequence_id = sequence_number

            if len(results) == limit:
                break

        return results, last_sequence_id

    def put_record(self, partition_key, data):
        # Note: this function is not safe for concurrency
        if self.records:
            last_sequence_number = self.get_max_sequence_number()
        else:
            last_sequence_number = 0
        sequence_number = last_sequence_number + 1
        self.records[sequence_number] = Record(partition_key, data, sequence_number)
        return sequence_number

    def get_min_sequence_number(self):
        if self.records:
            return list(self.records.keys())[0]
        return 0

    def get_max_sequence_number(self):
        if self.records:
            return list(self.records.keys())[-1]
        return 0

    def to_json(self):
        return {
            "HashKeyRange": {
                "EndingHashKey": "113427455640312821154458202477256070484",
                "StartingHashKey": "0"
            },
            "SequenceNumberRange": {
                "EndingSequenceNumber": self.get_max_sequence_number(),
                "StartingSequenceNumber": self.get_min_sequence_number(),
            },
            "ShardId": self.shard_id
        }


class Stream(object):
    def __init__(self, stream_name, shard_count, region):
        self.stream_name = stream_name
        self.shard_count = shard_count
        self.region = region
        self.account_number = "123456789012"
        self.shards = {}

        for index in range(shard_count):
            shard_id = "shardId-{0}".format(str(index).zfill(12))
            self.shards[shard_id] = Shard(shard_id)

    @property
    def arn(self):
        return "arn:aws:kinesis:{region}:{account_number}:{stream_name}".format(
            region=self.region,
            account_number=self.account_number,
            stream_name=self.stream_name
        )

    def get_shard(self, shard_id):
        if shard_id in self.shards:
            return self.shards[shard_id]
        else:
            raise ShardNotFoundError(shard_id)

    def get_shard_for_key(self, partition_key):
        # TODO implement sharding
        shard = list(self.shards.values())[0]
        return shard

    def put_record(self, partition_key, explicit_hash_key, sequence_number_for_ordering, data):
        partition_key = explicit_hash_key if explicit_hash_key else partition_key
        shard = self.get_shard_for_key(partition_key)

        sequence_number = shard.put_record(partition_key, data)
        return sequence_number, shard.shard_id

    def to_json(self):
        return {
            "StreamDescription": {
                "StreamARN": self.arn,
                "StreamName": self.stream_name,
                "StreamStatus": "ACTIVE",
                "HasMoreShards": False,
                "Shards": [shard.to_json() for shard in self.shards.values()],
            }
        }


class FirehoseRecord(object):
    def __init__(self, record_data):
        self.record_id = 12345678
        self.record_data = record_data


class DeliveryStream(object):
    def __init__(self, stream_name, **stream_kwargs):
        self.name = stream_name
        self.redshift_username = stream_kwargs['redshift_username']
        self.redshift_password = stream_kwargs['redshift_password']
        self.redshift_jdbc_url = stream_kwargs['redshift_jdbc_url']
        self.redshift_role_arn = stream_kwargs['redshift_role_arn']
        self.redshift_copy_command = stream_kwargs['redshift_copy_command']

        self.redshift_s3_role_arn = stream_kwargs['redshift_s3_role_arn']
        self.redshift_s3_bucket_arn = stream_kwargs['redshift_s3_bucket_arn']
        self.redshift_s3_prefix = stream_kwargs['redshift_s3_prefix']
        self.redshift_s3_compression_format = stream_kwargs.get('redshift_s3_compression_format', 'UNCOMPRESSED')
        self.redshift_s3_buffering_hings = stream_kwargs['redshift_s3_buffering_hings']

        self.records = []
        self.status = 'ACTIVE'
        self.create_at = datetime.datetime.utcnow()
        self.last_updated = datetime.datetime.utcnow()

    @property
    def arn(self):
        return 'arn:aws:firehose:us-east-1:123456789012:deliverystream/{0}'.format(self.name)

    def to_dict(self):
        return {
            "DeliveryStreamDescription": {
                "CreateTimestamp": time.mktime(self.create_at.timetuple()),
                "DeliveryStreamARN": self.arn,
                "DeliveryStreamName": self.name,
                "DeliveryStreamStatus": self.status,
                "Destinations": [
                    {
                        "DestinationId": "string",
                        "RedshiftDestinationDescription": {
                            "ClusterJDBCURL": self.redshift_jdbc_url,
                            "CopyCommand": self.redshift_copy_command,
                            "RoleARN": self.redshift_role_arn,
                            "S3DestinationDescription": {
                                "BucketARN": self.redshift_s3_bucket_arn,
                                "BufferingHints": self.redshift_s3_buffering_hings,
                                "CompressionFormat": self.redshift_s3_compression_format,
                                "Prefix": self.redshift_s3_prefix,
                                "RoleARN": self.redshift_s3_role_arn
                            },
                            "Username": self.redshift_username,
                        },
                    }
                ],
                "HasMoreDestinations": False,
                "LastUpdateTimestamp": time.mktime(self.last_updated.timetuple()),
                "VersionId": "string",
            }
        }

    def put_record(self, record_data):
        record = FirehoseRecord(record_data)
        self.records.append(record)
        return record


class KinesisBackend(BaseBackend):

    def __init__(self):
        self.streams = {}
        self.delivery_streams = {}

    def create_stream(self, stream_name, shard_count, region):
        if stream_name in self.streams:
           return ResourceInUseError(stream_name)
        stream = Stream(stream_name, shard_count, region)
        self.streams[stream_name] = stream
        return stream

    def describe_stream(self, stream_name):
        if stream_name in self.streams:
            return self.streams[stream_name]
        else:
            raise StreamNotFoundError(stream_name)

    def list_streams(self):
        return self.streams.values()

    def delete_stream(self, stream_name):
        if stream_name in self.streams:
            return self.streams.pop(stream_name)
        raise StreamNotFoundError(stream_name)

    def get_shard_iterator(self, stream_name, shard_id, shard_iterator_type, starting_sequence_number):
        # Validate params
        stream = self.describe_stream(stream_name)
        shard = stream.get_shard(shard_id)

        shard_iterator = compose_new_shard_iterator(
            stream_name, shard, shard_iterator_type, starting_sequence_number
        )
        return shard_iterator

    def get_records(self, shard_iterator, limit):
        decomposed = decompose_shard_iterator(shard_iterator)
        stream_name, shard_id, last_sequence_id = decomposed

        stream = self.describe_stream(stream_name)
        shard = stream.get_shard(shard_id)

        records, last_sequence_id = shard.get_records(last_sequence_id, limit)

        next_shard_iterator = compose_shard_iterator(stream_name, shard, last_sequence_id)

        return next_shard_iterator, records

    def put_record(self, stream_name, partition_key, explicit_hash_key, sequence_number_for_ordering, data):
        stream = self.describe_stream(stream_name)

        sequence_number, shard_id = stream.put_record(
            partition_key, explicit_hash_key, sequence_number_for_ordering, data
        )

        return sequence_number, shard_id

    def put_records(self, stream_name, records):
        stream = self.describe_stream(stream_name)

        response = {
            "FailedRecordCount": 0,
            "Records" : []
        }

        for record in records:
            partition_key = record.get("PartitionKey")
            explicit_hash_key = record.get("ExplicitHashKey")
            data = record.get("data")

            sequence_number, shard_id = stream.put_record(
                partition_key, explicit_hash_key, None, data
            )
            response['Records'].append({
                "SequenceNumber": sequence_number,
                "ShardId": shard_id
            })

        return response

    ''' Firehose '''
    def create_delivery_stream(self, stream_name, **stream_kwargs):
        stream = DeliveryStream(stream_name, **stream_kwargs)
        self.delivery_streams[stream_name] = stream
        return stream

    def get_delivery_stream(self, stream_name):
        if stream_name in self.delivery_streams:
            return self.delivery_streams[stream_name]
        else:
            raise StreamNotFoundError(stream_name)

    def list_delivery_streams(self):
        return self.delivery_streams.values()

    def delete_delivery_stream(self, stream_name):
        self.delivery_streams.pop(stream_name)

    def put_firehose_record(self, stream_name, record_data):
        stream = self.get_delivery_stream(stream_name)
        record = stream.put_record(record_data)
        return record

kinesis_backends = {}
for region in boto.kinesis.regions():
    kinesis_backends[region.name] = KinesisBackend()
