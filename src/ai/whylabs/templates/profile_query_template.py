import logging
import argparse
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple, cast

import apache_beam as beam
import pandas as pd
from apache_beam.io import WriteToText, ReadFromBigQuery
from apache_beam.options.pipeline_options import PipelineOptions, SetupOptions
from apache_beam.typehints.batch import BatchConverter, ListBatchConverter
from whylogs.core import DatasetProfile, DatasetProfileView

# matches PROJECT:DATASET.TABLE.
table_ref_regex = re.compile(r'[^:\.]+:[^:\.]+\.[^:\.]+')


@dataclass
class TemplateArgs():
    org_id: str
    output: str
    input: str
    api_key: str
    dataset_id: str
    logging_level: str
    date_column: str
    date_grouping_frequency: str


class ProfileIndex():
    """
    Abstraction around the type of thing that we return from profiling rows. It represents
    a dictionary of dataset timestamp to whylogs ResultSet.
    """

    def __init__(self, index: Dict[str, DatasetProfileView] = {}) -> None:
        self.index: Dict[str, DatasetProfileView] = index

    def get(self, date_str: str) -> Optional[DatasetProfileView]:
        return self.index[date_str]

    def set(self, date_str: str, view: DatasetProfileView):
        self.index[date_str] = view

    def tuples(self) -> List[Tuple[str, DatasetProfileView]]:
        return list(self.index.items())

    # Mutates
    def merge_index(self, other: 'ProfileIndex') -> 'ProfileIndex':
        for date_str, view in other.index.items():
            self.merge(date_str, view)

        return self

    def merge(self, date_str: str, view: DatasetProfileView):
        if date_str in self.index:
            self.index[date_str] = self.index[date_str].merge(view)
        else:
            self.index[date_str] = view

    def estimate_size(self) -> int:
        return sum(map(len, self.extract().values()))

    def __len__(self) -> int:
        return len(self.index)

    def __iter__(self):
        # The runtime wants to use this to estimate the size of the object,
        # I suppose to load balance across workers.
        return self.extract().values().__iter__()

    def __getstate__(self):
        return self.__dict__

    def __setstate__(self, d):
        self.__dict__ = d

    def upload_to_whylabs(self, logger: logging.Logger, org_id: str, api_key: str, dataset_id: str):
        # TODO ResultSets only have DatasetProfiles, not DatasetProfileViews, I can't use them here.
        #   But then how does this work with spark? How are they writing their profiles there, or do they
        #   just export them and write them externally? If they export the profiles, do they only do that
        #   becaues they can't write it from the cluster if they wanted to?
        from whylogs.api.writer.whylabs import WhyLabsWriter
        writer = WhyLabsWriter(org_id=org_id, api_key=api_key, dataset_id=dataset_id)

        for date_str, view in self.index.items():
            logger.info("Writing dataset profile to %s:%s for timestamp %s", org_id, dataset_id, date_str)
            writer.write(view)

    def extract(self) -> Dict[str, bytes]:
        out: Dict[str, bytes] = {}
        for date_str, view in self.index.items():
            out[date_str] = view.serialize()
        return out


class WhylogsProfileIndexMerger(beam.CombineFn):
    def __init__(self, args: TemplateArgs):
        self.logger = logging.getLogger("WhylogsProfileIndexMerger")
        self.logging_level = args.logging_level

    def setup(self):
        self.logger.setLevel(logging.getLevelName(self.logging_level))

    def create_accumulator(self) -> ProfileIndex:
        return ProfileIndex()

    def add_input(self, accumulator: ProfileIndex, input: ProfileIndex) -> ProfileIndex:
        return accumulator.merge_index(input)

    def add_inputs(self, mutable_accumulator: ProfileIndex, elements: List[ProfileIndex]) -> ProfileIndex:
        count = 0
        for current_index in elements:
            mutable_accumulator.merge_index(current_index)
            count = count + 1
        self.logger.debug("adding %s inputs", count)
        return mutable_accumulator

    def merge_accumulators(self, accumulators: List[ProfileIndex]) -> ProfileIndex:
        acc = ProfileIndex()
        count = 0
        for current_index in accumulators:
            acc.merge_index(current_index)
            count = count + 1
        self.logger.debug("merging %s views", count)
        return acc

    def extract_output(self, accumulator: ProfileIndex) -> ProfileIndex:
        return accumulator


class UploadToWhylabsFn(beam.DoFn):
    def __init__(self, args: TemplateArgs):
        self.args = args
        self.logger = logging.getLogger("UploadToWhylabsFn")

    def setup(self):
        self.logger.setLevel(logging.getLevelName(self.args.logging_level))

    def process_batch(self, batch: List[ProfileIndex]) -> Iterator[List[ProfileIndex]]:
        for index in batch:
            index.upload_to_whylabs(self.logger,
                                    self.args.org_id,
                                    self.args.api_key,
                                    self.args.dataset_id)
        yield batch


class ProfileDoFn(beam.DoFn):
    def __init__(self, args: TemplateArgs):
        self.date_column = args.date_column
        self.freq = args.date_grouping_frequency
        self.logging_level = args.logging_level
        self.logger = logging.getLogger("ProfileDoFn")

    def setup(self):
        self.logger.setLevel(logging.getLevelName(self.logging_level))

    def _process_batch_without_date(self, batch: List[Dict[str, Any]]) -> Iterator[List[DatasetProfileView]]:
        self.logger.debug("Processing batch of size %s", len(batch))
        profile = DatasetProfile()
        profile.track(pd.DataFrame.from_dict(batch))
        yield [profile.view()]

    def _process_batch_with_date(self, batch: List[Dict[str, Any]]) -> Iterator[List[ProfileIndex]]:
        tmp_date_col = '_whylogs_datetime'
        df = pd.DataFrame(batch)
        df[tmp_date_col] = pd.to_datetime(df[self.date_column])
        grouped = df.set_index(tmp_date_col).groupby(pd.Grouper(freq=self.freq))

        profiles = ProfileIndex()
        for date_group, dataframe in grouped:
            # pandas includes every date in the range, not just the ones that had rows...
            # TODO find out how to make the dataframe exclude empty entries instead
            if len(dataframe) == 0:
                continue

            ts = date_group.to_pydatetime()
            profile = DatasetProfile(dataset_timestamp=ts)
            profile.track(dataframe)
            profiles.set(str(date_group), profile.view())

        self.logger.debug("Processing batch of size %s into %s profiles", len(
            batch), len(profiles))

        # TODO best way of returning this thing is pickle?
        yield [profiles]

    def process_batch(self, batch: List[Dict[str, Any]]) -> Iterator[List[ProfileIndex]]:
        return self._process_batch_with_date(batch)


def is_table_input(table_string: str) -> bool:
    return table_ref_regex.match(table_string) is not None


def resolve_table_input(input: str):
    return input if is_table_input(input) else None


def resolve_query_input(input: str):
    return None if is_table_input(input) else input


def serialize_index(index: ProfileIndex) -> List[bytes]:
    """
    This function converts a single ProfileIndex into a collection of
    serialized DatasetProfileViews so that they can subsequently be written
    individually to GCS, rather than as a giant collection that has to be
    parsed in a special way to get it back into a DatasetProfileView.
    """
    return list(index.extract().values())


class ProfileIndexBatchConverter(ListBatchConverter):
    # TODO why do I get an error around this stuff while uploading the template now?
    def estimate_byte_size(self, batch: List[ProfileIndex]):
        if len(batch) == 0:
            return 0

        return batch[0].estimate_size()


BatchConverter.register(ProfileIndexBatchConverter)


def run(argv=None, save_main_session=True):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--output',
        dest='output',
        required=True,
        help='Output file or gs:// path to write results to.')
    parser.add_argument(
        '--input',
        dest='input',
        required=True,
        help='This can be a SQL query that includes a table name or a fully qualified reference to a table with the form PROJECT:DATASET.TABLE')
    parser.add_argument(
        '--date_column',
        dest='date_column',
        required=True,
        help='The string name of the column that contains a datetime. The column should be of type TIMESTAMP in the SQL schema.')
    parser.add_argument(
        '--date_grouping_frequency',
        dest='date_grouping_frequency',
        default='D',
        help='One of the freq options in the pandas Grouper(freq=) API. D for daily, W for weekly, etc.')
    parser.add_argument(
        '--logging_level',
        dest='logging_level',
        default='INFO',
        help='One of the logging levels from the logging module.')
    parser.add_argument(
        '--org_id',
        dest='org_id',
        required=True,
        help='The WhyLabs organization id to write the result profiles to.')
    parser.add_argument(
        '--dataset_id',
        dest='dataset_id',
        required=True,
        help='The WhyLabs model id id to write the result profiles to. Must be in the provided organization.')
    parser.add_argument(
        '--api_key',
        dest='api_key',
        required=True,
        help='An api key for the organization. This can be generated from the Settings menu of your WhyLabs account.')

    known_args, pipeline_args = parser.parse_known_args(argv)
    pipeline_options = PipelineOptions(pipeline_args)
    pipeline_options.view_as(SetupOptions).save_main_session = save_main_session

    args = TemplateArgs(
        api_key=known_args.api_key,
        output=known_args.output,
        input=known_args.input,
        dataset_id=known_args.dataset_id,
        org_id=known_args.org_id,
        logging_level=known_args.logging_level,
        date_column=known_args.date_column,
        date_grouping_frequency=known_args.date_grouping_frequency)

    with beam.Pipeline(options=pipeline_options) as p:

        # The main pipeline
        result = (
            p
            | 'ReadTable' >> ReadFromBigQuery(query=args.input,
                                              use_standard_sql=True,
                                              method=ReadFromBigQuery.Method.DIRECT_READ)
            .with_output_types(Dict[str, Any])
            | 'Profile' >> beam.ParDo(ProfileDoFn(args))
            | 'Merge profiles' >> beam.CombineGlobally(WhylogsProfileIndexMerger(args))
            .with_output_types(ProfileIndex)
        )

        # A fork that uploads to WhyLabs
        result | 'Upload to WhyLabs' >> (beam.ParDo(UploadToWhylabsFn(args))
                                         .with_input_types(ProfileIndex)
                                         .with_output_types(ProfileIndex))

        # A fork that uploads to GCS, each dataset profile in serialized form, one per file.
        (result
         | 'Serialize Proflies' >> beam.ParDo(serialize_index)
            .with_input_types(ProfileIndex)
            .with_output_types(bytes)
         | 'Upload to GCS' >> WriteToText(args.output,
                                          max_records_per_shard=1,
                                          file_name_suffix=".bin")
         )


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    run()