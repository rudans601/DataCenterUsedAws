"""전용 EKS 관리형 노드 그룹에 속한 EC2 워커만 조회한다."""
import json
import os
import subprocess

from kubeops_copilot import metrics

CLUSTER = "datacenterusedaws-lab"
REGION = "ap-northeast-2"
NODE_GROUP = "scenario-workers"


def aws(*args):
    result = subprocess.run(
        ["aws", "--profile", os.environ["AWS_PROFILE"], "--region", REGION, *args,
         "--output", "json", "--no-cli-pager", "--cli-connect-timeout", "4", "--cli-read-timeout", "8"],
        check=True, capture_output=True, text=True, timeout=20,
    )
    return json.loads(result.stdout)


def inventory():
    cluster = aws("eks", "describe-cluster", "--name", CLUSTER)["cluster"]
    arn = f"arn:aws:eks:{REGION}:{os.environ['KUBEOPS_AWS_ACCOUNT']}:cluster/{CLUSTER}"
    if (cluster["arn"] != arn or cluster["endpoint"] != os.environ.get("KUBEOPS_EKS_VERIFIED_ENDPOINT")
            or cluster.get("tags", {}).get("Project") != "DataCenterUsedAws"
            or cluster.get("tags", {}).get("Purpose") != "scenario-lab"):
        raise ValueError("EKS 실습 대상이 일치하지 않습니다.")
    group = aws("eks", "describe-nodegroup", "--cluster-name", CLUSTER, "--nodegroup-name", NODE_GROUP)["nodegroup"]
    groups = [g["name"] for g in group["resources"]["autoScalingGroups"]]
    workers = []
    if groups:
        reservations = aws("ec2", "describe-instances", "--filters",
                           "Name=vpc-id,Values=" + cluster["resourcesVpcConfig"]["vpcId"],
                           "Name=tag:aws:autoscaling:groupName,Values=" + ",".join(groups),
                           "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down")["Reservations"]
        for reservation in reservations:
            for instance in reservation["Instances"]:
                workers.append({"EC2 인스턴스": instance["InstanceId"], "상태": instance["State"]["Name"],
                                "유형": instance["InstanceType"], "가용 영역": instance["Placement"]["AvailabilityZone"],
                                "노드 DNS": instance.get("PrivateDnsName", ""), "내부 IP": instance.get("PrivateIpAddress", "")})
    if workers:
        ready = {item["metric"]["node"]: item["value"][1] == "1" for item in
                 metrics.query('kube_node_status_condition{condition="Ready",status="true"}') if "node" in item["metric"]}
        for worker in workers:
            state = ready.get(worker["노드 DNS"])
            worker["Kubernetes Ready"] = "미수집" if state is None else ("Ready" if state else "NotReady")
    return {"version": cluster["version"], "status": cluster["status"], "group_status": group["status"],
            "desired": group["scalingConfig"]["desiredSize"], "workers": workers}
