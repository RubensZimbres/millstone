"""Integration tests for SV calling.
"""

import os
import time

from django.conf import settings
from djcelery_testworker.testcase import CeleryWorkerTestCase
import vcf

from main.models import AlignmentGroup
from main.models import Dataset
from main.models import ExperimentSample
from main.models import get_dataset_with_type
from main.models import Project
from main.models import User
from pipeline.pipeline_runner import run_pipeline
from pipeline.variant_calling import VARIANT_TOOL_PARAMS_MAP
from utils.import_util import copy_and_add_dataset_source
from utils.import_util import import_reference_genome_from_local_file
from variants.vcf_parser import extract_raw_data_dict


class TestSVPipeline(CeleryWorkerTestCase):

    def setUp(self):
        user = User.objects.create_user('test_username_sv', password='password',
                email='test@example.com')

        # Grab a project.
        self.project = Project.objects.create(title='test project',
                owner=user.get_profile())

        # Use genome with deletion from our sv testing repo:
        # https://github.com/churchlab/structural-variants-testing
        DELETION_TEST_DATA_DIR = os.path.join(settings.PWD, 'test_data',
                'sv_testing', 'deletion_bd5a1123')
        REF = os.path.join(DELETION_TEST_DATA_DIR, 'small_ref.fa')
        FASTQ1 = os.path.join(DELETION_TEST_DATA_DIR, 'deletion_bd5a1123.1.fq')
        FASTQ2 = os.path.join(DELETION_TEST_DATA_DIR, 'deletion_bd5a1123.2.fq')

        # Create Datasets / import data.
        self.reference_genome = import_reference_genome_from_local_file(
                self.project, 'ref_genome', REF, 'fasta')
        self.experiment_sample = ExperimentSample.objects.create(
                project=self.project, label='sample1')
        copy_and_add_dataset_source(self.experiment_sample, Dataset.TYPE.FASTQ1,
                Dataset.TYPE.FASTQ1, FASTQ1)
        copy_and_add_dataset_source(self.experiment_sample, Dataset.TYPE.FASTQ2,
                Dataset.TYPE.FASTQ2, FASTQ2)

    def test_pipeline_and_svs(self):
        alignment_group_obj, async_result = run_pipeline(
                'name', self.reference_genome, [self.experiment_sample])

        # Block until pipeline finishes.
        while not async_result.ready():
            time.sleep(1)
        if async_result.status == 'FAILURE':
            self.fail('Async task failed.')

        # Get fresh copy of AlignmentGroup object since it was processed
        # different thread.
        alignment_group_obj = AlignmentGroup.objects.get(
                id=alignment_group_obj.id)

        self.assertEqual(1,
                len(alignment_group_obj.experimentsampletoalignment_set.all()))
        self.assertEqual(AlignmentGroup.STATUS.COMPLETED,
                alignment_group_obj.status)

        # Make sure the initial JBrowse config has been created.
        jbrowse_dir = self.reference_genome.get_jbrowse_directory_path()
        self.assertTrue(os.path.exists(jbrowse_dir))
        self.assertTrue(os.path.exists(os.path.join(jbrowse_dir,
                'indiv_tracks')))

        vcf_files = {}

        vcf_types = [VARIANT_TOOL_PARAMS_MAP[tool]['dataset_type']
                for tool in settings.ENABLED_VARIANT_CALLERS]

        for vcf_type in vcf_types:
            vcf_dataset = get_dataset_with_type(alignment_group_obj, vcf_type)
            self.assertIsNotNone(vcf_dataset,
                    msg='Missing vcf_dataset for {vcf_type}.'.format(
                            vcf_type=vcf_type))
            vcf_location = vcf_dataset.get_absolute_location()
            self.assertTrue(os.path.exists(vcf_location))
            vcf_files[vcf_type] = vcf_location

        # Check actual variants, with this helper vcf-parser function
        def get_variants(vcf_type):
            variants = []
            with open(vcf_files[vcf_type]) as fh:
                vcf_reader = vcf.Reader(fh)
                for record_idx, record in enumerate(vcf_reader):

                    raw_data_dict = extract_raw_data_dict(record)

                    # we should expect exactly 1 alternate
                    assert len(raw_data_dict['INFO_SVLEN']) == 1, (
                        'length of INFO_SVLEN > 1: {svlen}'.format(
                                svlen=raw_data_dict['INFO_SVLEN']))
                    assert len(raw_data_dict['INFO_SVTYPE']) == 1, (
                        'length of INFO_SVLEN > 1: {svtype}'.format(
                                svtype=raw_data_dict['INFO_SVTYPE']))

                    variant_type = str(raw_data_dict.get('INFO_SVTYPE',
                            raw_data_dict.get('TYPE'))[0])
                    pos = int(raw_data_dict.get('POS'))
                    length = int(raw_data_dict.get('INFO_SVLEN')[0])
                    variants.append({
                        'type': variant_type,
                        'pos': pos,
                        'length': length
                        })
            return variants

        lumpy_variants = get_variants(Dataset.TYPE.VCF_LUMPY)

        # Helper function for checking a specific variant type
        def verify_variant_type(variants, variant_type, pos, length):
            for variant in variants:
                # Check variant against following gauntlet.
                if variant['type'] != variant_type:
                    continue # Fail, incorrect type.
                if abs(abs(variant['pos']) - pos) >= 100:
                    continue # Fail, incorrect position.
                if (length != -1 and
                        abs(abs(variant['length']) - length) >= 100):
                    continue # Fail, incorrect length.
                # Success, variant made it through gauntlet.
                return

            # If we got here, no matches were found, fail.
            self.fail('No %s position %s found' % (variant_type, pos))

        verify_variant_type(lumpy_variants, 'DEL', 10000, 1000)
