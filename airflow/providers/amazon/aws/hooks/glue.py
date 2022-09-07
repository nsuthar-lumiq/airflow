#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import time
from typing import Dict, List, Optional

import boto3

from airflow.exceptions import AirflowException
from airflow.providers.amazon.aws.hooks.base_aws import AwsBaseHook


DEFAULT_LOG_SUFFIX = 'output'
FAILURE_LOG_SUFFIX = 'error'

# A filter value of ' ' translates to "match all".
# see: https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/FilterAndPatternSyntax.html

DEFAULT_LOG_FILTER = ' '
FAILURE_LOG_FILTER = '?ERROR ?Exception'

class GlueJobHook(AwsBaseHook):
    JOB_POLL_INTERVAL = 6  # polls job status after every JOB_POLL_INTERVAL seconds
    def __init__(
        self,
        s3_bucket: Optional[str] = None,
        job_name: Optional[str] = None,
        desc: Optional[str] = None,
        concurrent_run_limit: int = 1,
        script_location: Optional[str] = None,
        retry_limit: int = 0,
        num_of_dpus: Optional[int] = None,
        iam_role_name: Optional[str] = None,
        create_job_kwargs: Optional[dict] = None,
        *args,
        **kwargs,
    ):
        self.job_name = job_name
        self.desc = desc
        self.concurrent_run_limit = concurrent_run_limit
        self.script_location = script_location
        self.retry_limit = retry_limit
        self.s3_bucket = s3_bucket
        self.role_name = iam_role_name
        self.s3_glue_logs = 'logs/glue-logs/'
        self.create_job_kwargs = create_job_kwargs or {}

        worker_type_exists = "WorkerType" in self.create_job_kwargs
        num_workers_exists = "NumberOfWorkers" in self.create_job_kwargs

        if worker_type_exists and num_workers_exists:
            if num_of_dpus is not None:
                raise ValueError("Cannot specify num_of_dpus with custom WorkerType")
        elif not worker_type_exists and num_workers_exists:
            raise ValueError("Need to specify custom WorkerType when specifying NumberOfWorkers")
        elif worker_type_exists and not num_workers_exists:
            raise ValueError("Need to specify NumberOfWorkers when specifying custom WorkerType")
        elif num_of_dpus is None:
            self.num_of_dpus = 10
        else:
            self.num_of_dpus = num_of_dpus

        kwargs['client_type'] = 'glue'
        super().__init__(*args, **kwargs)

    def list_jobs(self) -> List:
        """:return: Lists of Jobs"""
        conn = self.get_conn()
        return conn.get_jobs()

    def get_iam_execution_role(self) -> Dict:
        """:return: iam role for job execution"""
        try:
            iam_client = self.get_session(region_name=self.region_name).client(
                'iam', endpoint_url=self.conn_config.endpoint_url, config=self.config, verify=self.verify
            )
            glue_execution_role = iam_client.get_role(RoleName=self.role_name)
            self.log.info("Iam Role Name: %s", self.role_name)
            return glue_execution_role
        except Exception as general_error:
            self.log.error("Failed to create aws glue job, error: %s", general_error)
            raise

    def initialize_job(
        self,
        script_arguments: Optional[dict] = None,
        run_kwargs: Optional[dict] = None,
    ) -> Dict[str, str]:
        """
        Initializes connection with AWS Glue
        to run job
        :return:
        """
        glue_client = self.get_conn()

        script_arguments = script_arguments or {}
        run_kwargs = run_kwargs or {}

        try:
            job_name = self.get_or_create_glue_job()
            return glue_client.start_job_run(JobName=job_name, Arguments=script_arguments, **run_kwargs)

        except Exception as general_error:
            self.log.error("Failed to run aws glue job, error: %s", general_error)
            raise

    def get_job_state(self, job_name: str, run_id: str) -> str:
        """
        Get state of the Glue job. The job state can be
        running, finished, failed, stopped or timeout.
        :param job_name: unique job name per AWS account
        :param run_id: The job-run ID of the predecessor job run
        :return: State of the Glue job
        """
        glue_client = self.get_conn()
        job_run = glue_client.get_job_run(JobName=job_name, RunId=run_id, PredecessorsIncluded=True)
        return job_run['JobRun']['JobRunState']


    def print_job_logs(
        self,
        job_name: str,
        run_id: str,
        log_client,
        job_failed: bool = False,
        next_token: Optional[str] = None,
    ) -> Optional[str]:
        """Prints the batch of logs to the Airflow task log and returns nextToken."""

        response = {}

        # filter_pattern = FAILURE_LOG_FILTER if job_failed else DEFAULT_LOG_FILTER
        log_group_prefix = self.conn.get_job_run(JobName=job_name, RunId=run_id)['JobRun']['LogGroupName']
        log_group_suffix = FAILURE_LOG_SUFFIX if job_failed else DEFAULT_LOG_SUFFIX
        log_group_name = f'{log_group_prefix}/{log_group_suffix}'
        try:
            self.log.info('Glue Job Run Logs')
            response = log_client.get_log_events(
                logGroupName=log_group_name,
                logStreamName=run_id
            )
            while len(response['events']) > 0:
                next_token = response["nextForwardToken"]
                messages = '\t'.join([event['message'] for event in response['events']])
                self.log.info('\n\t%s', messages)
                response = log_client.get_log_events(
                    logGroupName=log_group_name,
                    logStreamName=run_id,
                    nextToken=next_token
                )
        except log_client.exceptions.ResourceNotFoundException:
            self.log.warning(
                'No new Glue driver logs found. This might be because there are no new logs, '
                'or might be an error.\nIf the error persists, check the CloudWatch dashboard '
                f'at: https://{self.conn_region_name}.console.aws.amazon.com/cloudwatch/home'
            )

        # If no new log events are available, filter_log_events will return None.
        # In that case, check the same token again next pass.
        return response.get('nextToken') or next_token


    def print_output_logs(
            self,
            job_name: str,
            run_id: str,
            log_group_name: str,
            log_client,
            next_token = None
    ) -> Optional[str]:
        """Prints the batch of logs to the Airflow task log and returns nextToken."""
        log_group_name = log_group_name + '/' + DEFAULT_LOG_SUFFIX
        try:
            if next_token is None:
                response = log_client.get_log_events(logGroupName=log_group_name,
                                                     logStreamName=run_id)
            else:
                response = log_client.get_log_events(logGroupName=log_group_name,
                                                     logStreamName=run_id,
                                                     nextToken=next_token)
            if len(response['events']) > 0:
                for event in response['events']:
                    self.log.info("[{}]:: {}".format(str(job_name),event['message']))
                return response["nextForwardToken"]
            else:
                return None
        except log_client.exceptions.ResourceNotFoundException:
            self.log.warning("Waiting for Log Streaming...")
            return None
        except Exception as E:
            self.log.warning(str(E))
            return None


    def job_completion(self, job_name: str,
                       run_id: str,
                       verbose: bool = False,
                       continuous_logging: bool = False) -> Dict[str, str]:
        """
        Waits until Glue job with job_name completes or
        fails and return final state if finished.
        Raises AirflowException when the job failed
        :param job_name: unique job name per AWS account
        :param run_id: The job-run ID of the predecessor job run
        :param verbose: If True, more Glue Job Run logs show in the Airflow Task Logs.  (default: False)
        :return: Dict of JobRunState and JobRunId
        print continue output logs from cloudwatch
        """
        region_name = self.conn_region_name
        self.log.info("region  name  - {}".format(str(region_name)))
        credentials = self.get_credentials(region_name=region_name)
        failed_states = ['FAILED', 'TIMEOUT']
        finished_states = ['SUCCEEDED', 'STOPPED']
        log_client = boto3.client('logs',
                                  region_name=region_name,
                                  aws_access_key_id=credentials.access_key,
                                  aws_secret_access_key=credentials.secret_key)
        glue_client = self.get_conn()
        next_log_token = None
        mode = None
        job_failed = False
        if verbose and continuous_logging:
            mode = 'CONTINUE'
            self.log.info("Continuous logging mode is enable.")
        elif verbose:
            mode = 'BATCH'
            self.log.info("Batch logging mode is enable.")
        else:
            self.log.info("Logging is disable.")

        while True:
            try:
                job_run = glue_client.get_job_run(JobName=job_name, RunId=run_id, PredecessorsIncluded=True)
                job_run_state = job_run['JobRun']['JobRunState']
                Msg = f'[Batch] The Job {job_name} with runid {run_id} is : {job_run_state}'
                if mode == 'CONTINUE':
                    LogGrpName =  job_run['JobRun']['LogGroupName']
                    Token  = self.print_output_logs(job_name=job_name,
                                                    run_id = run_id,
                                                    log_group_name =  LogGrpName,
                                                    log_client = log_client,
                                                    next_token=next_log_token
                                                     )
                    while Token is not None:
                        next_log_token = Token
                        Token = self.print_output_logs(job_name=job_name,
                                                       run_id=run_id,
                                                       log_group_name=LogGrpName,
                                                       log_client=log_client,
                                                       next_token=next_log_token)
                elif mode == 'BATCH':
                    Msg = f'[LogMode: Batch] The Job {job_name} with runid {run_id} is : {job_run_state}'
                    self.log.info(Msg)
                else:
                    self.log.info(Msg)
                if job_run_state in failed_states:
                    job_failed = True
                    if mode == 'BATCH':
                        next_log_token = self.print_job_logs(
                            job_name=job_name,
                            run_id=run_id,
                            log_client=log_client,
                            job_failed=job_failed,
                            next_token=next_log_token,
                        )
                    break
                elif job_run_state in finished_states:
                    if mode == 'BATCH':
                        next_log_token = self.print_job_logs(
                            job_name=job_name,
                            run_id=run_id,
                            log_client=log_client,
                            job_failed=job_failed,
                            next_token=next_log_token,
                        )
                    return {'JobRunState': job_run_state, 'JobRunId': run_id}
                else:
                    time.sleep(self.JOB_POLL_INTERVAL)
            except Exception as e:
                self.log.error(str(e))
        raise AirflowException(Msg)

    def get_or_create_glue_job(self) -> str:
        """
        Creates(or just returns) and returns the Job name
        :return:Name of the Job
        """
        glue_client = self.get_conn()
        try:
            get_job_response = glue_client.get_job(JobName=self.job_name)
            self.log.info("Job Already exist. Returning Name of the job")
            return get_job_response['Job']['Name']

        except glue_client.exceptions.EntityNotFoundException:
            self.log.info("Job doesn't exist. Now creating and running AWS Glue Job")
            if self.s3_bucket is None:
                raise AirflowException('Could not initialize glue job, error: Specify Parameter `s3_bucket`')
            s3_log_path = f's3://{self.s3_bucket}/{self.s3_glue_logs}{self.job_name}'
            execution_role = self.get_iam_execution_role()
            try:
                default_command = {
                    "Name": "glueetl",
                    "ScriptLocation": self.script_location,
                }
                command = self.create_job_kwargs.pop("Command", default_command)

                if "WorkerType" in self.create_job_kwargs and "NumberOfWorkers" in self.create_job_kwargs:
                    create_job_response = glue_client.create_job(
                        Name=self.job_name,
                        Description=self.desc,
                        LogUri=s3_log_path,
                        Role=execution_role['Role']['Arn'],
                        ExecutionProperty={"MaxConcurrentRuns": self.concurrent_run_limit},
                        Command=command,
                        MaxRetries=self.retry_limit,
                        **self.create_job_kwargs,
                    )
                else:
                    create_job_response = glue_client.create_job(
                        Name=self.job_name,
                        Description=self.desc,
                        LogUri=s3_log_path,
                        Role=execution_role['Role']['Arn'],
                        ExecutionProperty={"MaxConcurrentRuns": self.concurrent_run_limit},
                        Command=command,
                        MaxRetries=self.retry_limit,
                        MaxCapacity=self.num_of_dpus,
                        **self.create_job_kwargs,
                    )
                return create_job_response['Name']
            except Exception as general_error:
                self.log.error("Failed to create aws glue job, error: %s", general_error)
                raise
