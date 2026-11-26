"""Maintain only the tagged TCP/8944 group of the Page V2 Hong Kong CVM."""
import argparse
from contextlib import contextmanager
import ipaddress
import json
from pathlib import Path
import re
import time

from inventory import PRIVATE_V4, VPC, cloud_request, write_inventory

INSTANCE = "ins-52tbyu6m"
PRIVATE_IP = "10.89.0.125"
NAME = "fb-page-v2-hk-8944"
DESCRIPTION = "FB Page V2 publisher ingress TCP 8944 for " + INSTANCE
TAGS = {"managed-by":"fb-page-v2", "target-instance":INSTANCE}
RULE = "fb-page-v2-publisher"
VERSION = "2017-03-12"
STATE = Path("/mnt/data-disk/fb-page-v2/network/security-group.json")


@contextmanager
def locked(path):
    import fcntl
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def desired_rules(addresses):
    rules = []
    for address in sorted(set(addresses), key=ipaddress.ip_address):
        ip = ipaddress.IPv4Address(address)
        if not any(ip in net for net in PRIVATE_V4):
            raise ValueError("non_private_publisher")
        rules.append({"Protocol":"TCP", "Port":"8944", "CidrBlock":str(ip)+"/32",
                      "Action":"ACCEPT", "PolicyDescription":RULE})
    return rules


def canonical_rules(policy):
    """Order is immaterial for these exact, disjoint ACCEPT /32 rules."""
    rows = []
    for rule in policy.get("Ingress") or []:
        raw = str(rule.get("CidrBlock") or "")
        if raw and "/" not in raw:
            raw += "/32"
        rows.append((str(rule.get("Protocol")).upper(),str(rule.get("Port")),raw,
                     str(rule.get("Action")).upper(),str(rule.get("PolicyDescription") or "")))
    return (sorted(rows), len(policy.get("Egress") or []))


class Network:
    def __init__(self, call, path, sleep=time.sleep):
        self.call, self.path, self.sleep = call, path, sleep
        self.state = json.loads(path.read_text()) if path.exists() else {}

    def save(self):
        write_inventory(self.path,self.state)

    def find(self, pinned=None):
        query = ({"SecurityGroupIds":[pinned]} if pinned else
                 {"Filters":[{"Name":"security-group-name","Values":[NAME]}],"Limit":"100"})
        payload = self.call("vpc","DescribeSecurityGroups",query)
        if int(payload["TotalCount"]) != len(payload["SecurityGroupSet"]):
            raise RuntimeError("ambiguous_security_group_listing")
        rows = [r for r in payload["SecurityGroupSet"] if pinned or r["SecurityGroupName"] == NAME]
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("ambiguous_managed_group")
        row = rows[0]
        gid = row.get("SecurityGroupId","")
        tags = {v["Key"]:v["Value"] for v in row.get("TagSet") or []}
        if (not re.fullmatch(r"sg-[a-z0-9]+",gid) or (pinned and gid != pinned)
                or row["SecurityGroupName"] != NAME or row.get("SecurityGroupDesc") != DESCRIPTION
                or row.get("IsDefault") or not TAGS.items() <= tags.items()):
            raise RuntimeError("security_group_ownership_mismatch")
        return gid

    def target(self):
        rows = self.call("cvm","DescribeInstances",{"InstanceIds":[INSTANCE]})["InstanceSet"]
        if (len(rows) != 1 or rows[0]["InstanceId"] != INSTANCE
                or rows[0].get("VirtualPrivateCloud",{}).get("VpcId") != VPC
                or PRIVATE_IP not in rows[0].get("PrivateIpAddresses",())):
            raise RuntimeError("target_instance_mismatch")
        return rows[0]

    def exclusive(self, gid, attached):
        rows = self.call("vpc","DescribeSecurityGroupAssociationStatistics",
                         {"SecurityGroupIds":[gid]})["SecurityGroupAssociationStatisticsSet"]
        if len(rows) != 1 or rows[0]["SecurityGroupId"] != gid:
            raise RuntimeError("association_statistics_unavailable")
        row = rows[0]
        # TotalCount excludes references and includes the CVM primary ENI once.
        # ENI by itself can also describe that primary interface and is not additive.
        if (int(row["TotalCount"]) != int(attached) or int(row["CVM"]) != int(attached)
                or int(row.get("SG",0)) != 0):
            raise RuntimeError("managed_group_has_unexpected_associations")

    def policies(self, gid):
        return self.call("vpc","DescribeSecurityGroupPolicies",{"SecurityGroupId":gid})["SecurityGroupPolicySet"]

    def wait_for(self, operation, check):
        failure = None
        try:
            operation()
        except (OSError, RuntimeError, ValueError) as exc:
            failure = exc
        for wait in (0,1,2,3,5):
            if wait:
                self.sleep(wait)
            try:
                result = check()
            except (OSError,RuntimeError,ValueError) as exc:
                if failure is None:
                    failure = exc
                continue
            if result:
                return result
        if failure:
            raise failure
        raise RuntimeError("cloud_operation_awaiting_readback")

    def group(self):
        pinned = self.state.get("group_id")
        found = self.find(pinned)
        if found:
            self.state.update(group_id=found,creating=False)
            self.save()
            return found
        if pinned:
            raise RuntimeError("recorded_security_group_missing")
        if self.state.get("creating"):
            raise RuntimeError("awaiting_previous_create_readback")
        self.state.update(creating=True)
        self.save()
        def create():
            try:
                out = self.call("vpc","CreateSecurityGroup",{
                    "GroupName":NAME,"GroupDescription":DESCRIPTION,
                    "Tags":[{"Key":k,"Value":v} for k,v in TAGS.items()]})
            except RuntimeError as exc:
                # Only definite request rejections can clear an unresolved creation.
                code = str(exc).removeprefix("inventory_")
                if code.startswith(("AuthFailure","UnauthorizedOperation","InvalidParameter","MissingParameter",
                                    "LimitExceeded","UnsupportedOperation","OperationDenied")):
                    self.state["creating"] = False
                    self.save()
                raise
            self.state["group_id"] = out["SecurityGroup"]["SecurityGroupId"]
            self.save()
        gid = self.wait_for(create, lambda: self.find(self.state.get("group_id")))
        self.state.update(group_id=gid,creating=False)
        self.save()
        return gid

    def associate(self, gid, attached, old):
        if (gid in old) == attached:
            return
        original = [g for g in old if g != gid]
        if not attached and not original:
            raise RuntimeError("refuse_to_remove_last_group")
        action = "AssociateSecurityGroups" if attached else "DisassociateSecurityGroups"
        def verified():
            ids = self.target()["SecurityGroupIds"]
            if [v for v in ids if v != gid] != original:
                raise RuntimeError("other_group_membership_changed")
            if (gid in ids) != attached:
                return False
            self.exclusive(gid,attached)
            return True
        self.wait_for(lambda: self.call("cvm",action,{"SecurityGroupIds":[gid],"InstanceIds":[INSTANCE]}),verified)

    def sync(self, addresses):
        if self.state.get("paused"):
            return {"paused":True,"group_id":self.state.get("group_id")}
        rules = desired_rules(addresses)
        current_ids = self.target()["SecurityGroupIds"]
        gid = self.group()
        self.exclusive(gid,gid in current_ids)
        if "original_groups" not in self.state:
            self.state["original_groups"] = [g for g in current_ids if g != gid]
            self.save()
        current = self.policies(gid)
        signature = canonical_rules({"Ingress":rules,"Egress":[]})
        if canonical_rules(current) != signature:
            version = str(current["Version"])
            if version == "0" and rules:
                # Version zero on Modify means clear all. Add to a fresh, empty group.
                if current.get("Ingress") or current.get("Egress"):
                    raise RuntimeError("unexpected_initial_policy_version")
                action, policy = "CreateSecurityGroupPolicies", {"Ingress":rules}
            else:
                action = "ModifySecurityGroupPolicies"
                policy = {"Version":version,"Ingress":rules,"Egress":[]}
            self.wait_for(lambda: self.call("vpc",action,{"SecurityGroupId":gid,"SecurityGroupPolicySet":policy}),
                          lambda: canonical_rules(self.policies(gid)) == signature)
        self.associate(gid,True,current_ids)
        final = self.target()["SecurityGroupIds"]
        self.exclusive(gid,True)
        if gid not in final or canonical_rules(self.policies(gid)) != signature:
            raise RuntimeError("managed_rules_readback_changed")
        return {"group_id":gid,"target":INSTANCE,"ingress":len(rules),"egress":0,"groups":final}

    def detach(self, expected):
        if self.state.get("group_id") != expected or self.find(expected) != expected:
            raise RuntimeError("rollback_group_mismatch")
        self.state["paused"]=True
        self.save()
        ids = self.target()["SecurityGroupIds"]
        self.associate(expected,False,ids)
        return {"paused":True,"group_id":expected,"groups":self.target()["SecurityGroupIds"]}


def manager(secret_path,state):
    return Network(lambda service,action,parameters: cloud_request(secret_path,service,VERSION,action,parameters),state)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("operation",choices=("status","detach","resume"))
    p.add_argument("--credentials",type=Path,required=True)
    p.add_argument("--state",type=Path,default=STATE)
    p.add_argument("--group-id")
    a=p.parse_args()
    with locked(a.state):
        net=manager(a.credentials,a.state)
        if a.operation=="detach":
            if not a.group_id:
                p.error("--group-id is required for detach")
            result=net.detach(a.group_id)
        elif a.operation=="resume":
            if not a.group_id or net.find(a.group_id)!=net.state.get("group_id"):
                raise RuntimeError("resume_group_mismatch")
            net.state["paused"]=False
            net.save()
            result={"resumed":a.group_id}
        else:
            result={"state":net.state,"current_groups":net.target()["SecurityGroupIds"]}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
