# Copyright (c) 2023, Xgrid Inc, https://xgrid.co

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#        http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import time
from datetime import date, timedelta

import botocore
import boto3
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway


try:
    s3 = boto3.client("s3")
except Exception as e:
    logging.error("Error creating boto3 client for s3: " + str(e))


def get_cost_and_usage_data(client, start, end, project_name=""):
    """
    Retrieves the unblended cost of a given account within a specified time period
    using the AWS Cost Explorer API.
    Args:
        client: A boto3.client object for the AWS Cost Explorer API.
        account_id: A string representing the AWS account ID to retrieve
        cost data for.
        region: A string representing the AWS Regionto retrieve cost data for.
        start_date: A string representing the start date of the time period to
        retrieve cost data for in YYYY-MM-DD format.
        end_date: A string representing the end date of the time period to
        retrieve cost data for in YYYY-MM-DD format.

    Returns:
        A dictionary representing the response from the AWS Cost Explorer API,
        containing the unblended cost of the specified services in specific project
        for the specified time period.
    Raises:
        ValueError: If there is a problem with the input data format,
        or if the calculation fails.
    """
    while True:
        try:
            response = client.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
                Filter={
                    "Tags": {
                        "Key": "Project",
                        "Values": [project_name],
                    }
                },
            )
            return response
        except client.exceptions.LimitExceededException:
            # Sleep for 5 seconds and try again

            time.sleep(5)
        except ValueError as ve:
            raise ValueError(
                f"ValueError occurred: {ve}.\nPlease check the input data format."
            )


def lambda_handler(event, context):
    """
    List 5 top most expensive services in provided aws region.
    Args:
        Project Name: Project name in aws.
    Returns:
        It pushes the 5 most expensive services name and cost with project name
        to Prometheus using push gateway.
    Raises:
        KeyError: Raise error if data not pushed to prometheus.
    """

    project_name = event["project_name"]

    cost_by_days = 30
    end_date = str(date.today())
    start_date = str(date.today() - timedelta(days=cost_by_days))

    parent_list = []

    try:
        ce = boto3.client("ce")
    except Exception as e:
        logging.error("Error creating boto3 client: " + str(e))

    # Retrieve the cost and usage data for the defined time period
    try:
        if project_name != "Others":
            cost_and_usage = get_cost_and_usage_data(
                ce, start_date, end_date, project_name
            )
        else:
            cost_and_usage = get_cost_and_usage_data(ce, start_date, end_date)
    except Exception as e:
        logging.error("Error getting response from cost and usage api: " + str(e))

    # Extract the cost data
    cost_data = cost_and_usage["ResultsByTime"][0]["Groups"]

    # Sort services_costs in descending order by cost
    sorted_services_costs = sorted(cost_data, key=lambda x: x["cost"], reverse=True)

    # Printing the sorted value
    for item in sorted_services_costs:
        resourcedata = {
            "Service": item["Keys"][0],
            "Cost": item["Metrics"]["UnblendedCost"]["Amount"],
        }
        parent_list.append(resourcedata)

    logging.info(parent_list)

    # Creating an empty list to store the data
    data_list = []

    # Adding the extracted cost data to the Prometheus
    # gauge as labels for service, region, and cost.
    try:
        registry = CollectorRegistry()
        project_name_for_gauge = project_name.replace("-", "_")
        gauge = Gauge(
            f"{project_name_for_gauge}_Services_Cost",
            "AWS Services Cost Detail",
            labelnames=["project_spend_services", "project_spend_cost"],
            registry=registry,
        )
        for i in range(len(parent_list)):
            service_name = parent_list[i]["Service"]
            cost_amount = parent_list[i]["Cost"]
            data_dict = {
                "Service": service_name,
                "Cost": cost_amount,
            }

            # add the dictionary to the list
            data_list.append(data_dict)
            gauge.labels(service_name, cost_amount).set(cost_amount)

            # Push the metric to the Prometheus Gateway

            push_to_gateway(
                os.environ["prometheus_ip"],
                job=f"{project_name}-Service",
                registry=registry,
            )
        # convert data to JSON
        json_data = json.dumps(data_list)
        print(data_list)

        # upload JSON file to S3 bucket
        bucket_name = os.environ["bucket_name"]
        key_name = f'{os.environ["project_breakdown_cost_prefix"]}/{project_name}.json'
        try:
            s3.put_object(Bucket=bucket_name, Key=key_name, Body=json_data)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchBucket":
                raise ValueError(f"Bucket not found: {os.environ['bucket_name']}")
            elif e.response["Error"]["Code"] == "AccessDenied":
                raise ValueError(
                    f"Access denied to S3 bucket: {os.environ['bucket_name']}"
                )
            else:
                raise ValueError(f"Failed to upload data to S3 bucket: {str(e)}")

    except Exception as e:
        logging.error("Error initializing Prometheus Registry and Gauge: " + str(e))
        return {"statusCode": 500, "body": json.dumps({"Error": str(e)})}
    # Return the response
    return {"statusCode": 200, "body": json.dumps(parent_list)}
