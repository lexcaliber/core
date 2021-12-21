import csv
import hashlib
import json
import os
from datetime import timezone
from pathlib import Path
from typing import Dict, List, Callable
import dateutil.parser
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from algorithms.helpers import format_reporter
from db.sqlalchemy import *
from db.sqlalchemy.models import Cluster, Opinion, Court, Citation
from helpers import get_full_path
from ingress.paths import BASE_CL_DIR, CLUSTER_PATH, OPINION_PATH, CITATIONS_PATH

DEFAULT_BATCH_SIZE = 10000


class DbUpdater:
    jurisdictions: List[Court]
    include_text_for: List[Court]
    force_update: bool
    session: Session

    def __init__(self, jurisdictions: List[Court], include_text_for: List[Court], force_update=False):
        self.jurisdictions = jurisdictions
        self.include_text_for = include_text_for
        self.force_update = force_update
        self.session = get_session()

    def update_from_cl_data(self):
        include_text_for_dict = {jur_name: True for jur_name in self.include_text_for}
        for jur_name in self.jurisdictions:
            include_text = include_text_for_dict.get(jur_name) or False
            print(f"Adding cluster data for jurisdiction {jur_name} to database...")
            self.process_cluster_data(self.__get_resource_dir_path(CLUSTER_PATH, jur_name), jurisdiction=jur_name)
            self.session.commit()
            print(f"Adding opinion data for jurisdiction {jur_name} to database...")
            self.process_opinion_data(self.__get_resource_dir_path(OPINION_PATH, jur_name), include_text=include_text, jurisdiction=jur_name)
            self.session.commit()
        print(f"Adding citation data to database...")
        self.process_citation_data(get_full_path(os.path.join(BASE_CL_DIR, CITATIONS_PATH)))
        self.session.commit()

    def __get_resource_dir_path(self, resource_type: str, jur_name: str):
        return get_full_path(os.path.join(BASE_CL_DIR, resource_type, jur_name))

    def process_cluster_data(self, dir_path: str, jurisdiction: str):
        cluster_checksum_dict = self.__get_cluster_checksum_dict()
        cluster_records = []
        directory = os.fsencode(dir_path)
        for file in os.listdir(directory):
            try:
                filename = os.fsdecode(file)
                if filename.endswith(".json"):
                    file_path = os.path.join(dir_path, filename)
                    with open(file_path, 'rb') as json_file:
                        file_contents = json_file.read()
                        file_checksum = hashlib.md5(file_contents).hexdigest()
                        resource_id = int(Path(filename).stem)
                        # If nothing about this resource has changed, we don't need to do anything.
                        if not self.force_update and cluster_checksum_dict.get(resource_id) == file_checksum:
                            continue

                        cluster_data = json.loads(file_contents.decode('utf-8'))
                        date_filed = dateutil.parser.parse(cluster_data['date_filed']).replace(tzinfo=timezone.utc)
                        reporter = self.__get_reporter(cluster_data)
                        new_record = dict(resource_id=cluster_data['id'],
                                          case_name=cluster_data['case_name'],
                                          cluster_uri=cluster_data['resource_uri'],
                                          docket_uri=cluster_data['docket'],
                                          citation_count=cluster_data['citation_count'],
                                          reporter=reporter,
                                          court=jurisdiction,
                                          year=date_filed.year,
                                          time=int(date_filed.timestamp()),
                                          courtlistener_json_checksum=file_checksum)
                        cluster_records.append(new_record)
            except:
                print(f'Failure on file {file}')
        print(f"Finished reading CL cluster data for jurisdiction {jurisdiction}, upserting {len(cluster_records)} records...")
        self.__batch_query(self.__upsert_clusters_to_db, cluster_records)

    def process_opinion_data(self, dir_path: str, include_text: bool, jurisdiction: str):
        opinion_checksum_dict = self.__get_opinion_checksum_dict()
        opinion_records = []
        directory = os.fsencode(dir_path)
        for file in os.listdir(directory):
            try:
                filename = os.fsdecode(file)
                if filename.endswith(".json"):
                    file_path = os.path.join(dir_path, filename)
                    with open(file_path, 'rb') as json_file:
                        file_contents = json_file.read()
                        file_checksum = hashlib.md5(file_contents).hexdigest()
                        resource_id = int(Path(filename).stem)
                        # If nothing about this resource has changed, we don't need to do anything.
                        if not self.force_update and opinion_checksum_dict.get(resource_id) == file_checksum:
                            continue

                        opinion_data = json.loads(file_contents.decode('utf-8'))
                        cluster_uri = opinion_data['cluster']
                        cluster_id = int(cluster_uri.split('/')[-2])
                        new_record = dict(resource_id=opinion_data['id'],
                                          opinion_uri=opinion_data['resource_uri'],
                                          cluster_uri=cluster_uri,
                                          cluster=cluster_id,
                                          courtlistener_json_checksum=file_checksum)
                        if include_text:
                            new_record.html_text = opinion_data['html_text']
                        opinion_records.append(new_record)
            except:
                print(f'Failure on file {file}')
        print(f"Finished reading CL opinion data for jurisdiction {jurisdiction}, upserting {len(opinion_records)} records...")
        batch_size = 100 if include_text else DEFAULT_BATCH_SIZE
        self.__batch_query(self.__upsert_opinions_to_db, opinion_records, batch_size=batch_size)

    def process_citation_data(self, citations_file):
        opinion_set = set(self.session.execute(select(Opinion.resource_id)).scalars().all())

        citation_records = []
        with open(citations_file) as csv_file:
            csv_reader = csv.reader(csv_file, delimiter=',')
            for row in csv_reader:
                try:
                    citing_opinion, cited_opinion, depth = int(row[0]), int(row[1]), int(row[2])
                    if citing_opinion in opinion_set and cited_opinion in opinion_set:
                        new_record = Citation(citing_opinion=citing_opinion, cited_opinion=cited_opinion,
                                              depth=depth)
                        citation_records.append(new_record)
                except Exception as e:
                    print(f'Failure on citation file row {row}: {e}')
            self.__batch_query(self.__upsert_citations_to_db, citation_records)

    def __get_reporter(self, cluster_data):
        reporters = cluster_data.get('citations')
        if reporters is None or len(reporters) == 0:
            return None
        reporter_to_use = reporters[0]
        for reporter in reporters[1:]:
            if reporter['reporter'] == 'U.S.':
                reporter_to_use = reporter
                break
        return format_reporter(volume=reporter_to_use['volume'], reporter=reporter_to_use['reporter'],
                               page=reporter_to_use['page'])

    def __batch_query(self, query_func: Callable, records: List[dict], batch_size=DEFAULT_BATCH_SIZE):
        num_batches = len(records) // batch_size
        if len(records) % batch_size != 0:
            num_batches += 1
        for i in range(num_batches):
            start_idx, end_idx = i * batch_size, (i + 1) * batch_size
            query_func(records[start_idx:end_idx])

    def __upsert_clusters_to_db(self, clusters: List[dict]):
        query = insert(Cluster).values(clusters)
        query = query.on_conflict_do_update(index_elements=[Cluster.resource_id],
                                            set_=dict(case_name=query.excluded.case_name,
                                                      docket_uri=query.excluded.docket_uri,
                                                      citation_count=query.excluded.citation_count,
                                                      reporter=query.excluded.reporter,
                                                      court=query.excluded.court,
                                                      year=query.excluded.year,
                                                      time=query.excluded.time,
                                                      courtlistener_json_checksum=query.excluded.courtlistener_json_checksum))
        self.session.execute(query)

    def __upsert_opinions_to_db(self, clusters: List[dict]):
        query = insert(Cluster).values(clusters)
        query = query.on_conflict_do_update(index_elements=[Opinion.resource_id],
                                            set_=dict(cluster_id=query.excluded.cluster_id,
                                                      cluster_uri=query.excluded.cluster_uri,
                                                      html_text=query.excluded.html_text,
                                                      courtlistener_json_checksum=query.excluded.courtlistener_json_checksum))
        self.session.execute(query)

    def __upsert_citations_to_db(self, citations: List[dict]):
        query = insert(Citation).values(citations)
        query = query.on_conflict_do_update(index_elements=[Citation.citing_opinion_id, Citation.cited_opinion_id],
                                            set_=dict(depth=query.excluded.depth))
        self.session.execute(query)

    def __get_cluster_checksum_dict(self) -> Dict[int, str]:
        res = self.session.execute(select(Cluster.resource_id, Cluster.courtlistener_json_checksum)).all()
        return {resource_id: checksum for resource_id, checksum in res}

    def __get_opinion_checksum_dict(self) -> Dict[int, str]:
        res = self.session.execute(select(Opinion.resource_id, Opinion.courtlistener_json_checksum)).all()
        return {resource_id: checksum for resource_id, checksum in res}


if __name__ == '__main__':
    updater = DbUpdater(jurisdictions=[Court.SCOTUS], include_text_for=[])
    updater.update_from_cl_data()