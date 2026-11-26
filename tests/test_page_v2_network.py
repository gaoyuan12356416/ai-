import json
from pathlib import Path
import sys
import tempfile
import unittest

ops = Path(__file__).resolve().parents[1]/"ops"/"fb-page-v2"
sys.path.insert(0,str(ops))
import inventory
import network


class Cloud:
    def __init__(self):
        self.group = None
        self.ids = ["sg-existing-1","sg-existing-2"]
        self.policy = {"Version":"0","Egress":[],"Ingress":[]}
        self.foreign = 0
        self.after = set()
        self.mutations = []

    def call(self, service, action, parameters):
        group = "sg-hkv2dedicated"
        if action == "DescribeInstances":
            self.scope = parameters["InstanceIds"]
            return {"InstanceSet":[dict(InstanceId=network.INSTANCE,VirtualPrivateCloud={"VpcId":inventory.VPC},
                    SecurityGroupIds=self.ids[:],PrivateIpAddresses=[network.PRIVATE_IP])]}
        if action == "DescribeSecurityGroups":
            rows = [self.group] if self.group else []
            return {"SecurityGroupSet":rows,"TotalCount":len(rows)}
        if action == "DescribeSecurityGroupAssociationStatistics":
            cnt = int(group in self.ids) + self.foreign
            return {"SecurityGroupAssociationStatisticsSet":[dict(SecurityGroupId=group,TotalCount=cnt,CVM=cnt,SG=0)]}
        if action == "DescribeSecurityGroupPolicies":
            return {"SecurityGroupPolicySet":self.policy}
        self.mutations.append((action,parameters))
        if action == "CreateSecurityGroup":
            self.group = dict(SecurityGroupId=group,SecurityGroupName=parameters["GroupName"],
                              SecurityGroupDesc=parameters["GroupDescription"],TagSet=parameters["Tags"])
            output = {"SecurityGroup":self.group}
        elif action in ("CreateSecurityGroupPolicies","ModifySecurityGroupPolicies"):
            assert parameters["SecurityGroupId"] == group
            policy = parameters["SecurityGroupPolicySet"]
            self.policy = dict(Version=str(int(self.policy["Version"])+1),Egress=policy.get("Egress",[]),
                               Ingress=[dict(v,Protocol=v["Protocol"].lower()) for v in policy["Ingress"]])
            output = {}
        elif action in ("AssociateSecurityGroups","DisassociateSecurityGroups"):
            assert parameters == {"InstanceIds":[network.INSTANCE],"SecurityGroupIds":[group]}
            if action == "AssociateSecurityGroups": self.ids.insert(0,group)
            else: self.ids.remove(group)
            output = {}
        else: raise AssertionError(action)
        if action in self.after:
            raise TimeoutError("simulated_response_loss")
        return output


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/"net"/"state.json"
        self.cloud = Cloud()
        self.manager = lambda: network.Network(self.cloud.call,self.path,lambda _:None)

    def test_scoped_initial_setup_and_updates_with_lost_ack(self):
        self.cloud.after={"CreateSecurityGroup","CreateSecurityGroupPolicies","AssociateSecurityGroups"}
        v = self.manager().sync(["10.89.0.20","10.89.0.21"])
        self.assertEqual(v["ingress"],2)
        self.assertEqual(v["egress"],0)
        self.assertEqual(self.cloud.ids[1:],["sg-existing-1","sg-existing-2"])
        self.assertEqual(json.loads(self.path.read_text())["original_groups"],self.cloud.ids[1:])
        before = len(self.cloud.mutations)
        self.manager().sync(["10.89.0.21","10.89.0.20"])
        self.assertEqual(len(self.cloud.mutations),before)
        self.cloud.after={"ModifySecurityGroupPolicies"}
        self.manager().sync(["10.89.0.21"])
        self.assertEqual([v["CidrBlock"] for v in self.cloud.policy["Ingress"]],["10.89.0.21/32"])
        self.assertEqual(self.cloud.policy["Egress"],[])
        self.manager().sync([])
        self.assertEqual(self.cloud.policy["Ingress"],[])
        self.assertTrue(all(v in ("TCP","tcp") for v in [x["Protocol"] for x in self.cloud.policy["Ingress"]]))

    def test_unmanaged_collision_and_later_foreign_binding_prevent_writes(self):
        self.manager().sync(["10.20.0.1"])
        count=len(self.cloud.mutations)
        self.cloud.foreign=1
        with self.assertRaisesRegex(RuntimeError,"unexpected_associations"):
            self.manager().sync(["10.20.0.2"])
        self.cloud.foreign=0
        self.cloud.group["TagSet"]=[]
        with self.assertRaisesRegex(RuntimeError,"ownership_mismatch"):
            self.manager().sync([])
        self.assertEqual(len(self.cloud.mutations),count)

    def test_recorded_ambiguous_create_and_scoped_detach(self):
        self.path.parent.mkdir()
        self.path.write_text('{"creating":true}')
        with self.assertRaisesRegex(RuntimeError,"previous_create"):
            self.manager().sync([])
        self.assertEqual(self.cloud.mutations,[])
        self.path.unlink()
        v=self.manager().sync(["10.0.0.1"])
        with self.assertRaisesRegex(RuntimeError,"rollback_group_mismatch"):
            self.manager().detach("sg-existing-1")
        self.cloud.after={"DisassociateSecurityGroups"}
        self.manager().detach(v["group_id"])
        self.assertEqual(self.cloud.ids,["sg-existing-1","sg-existing-2"])
        self.assertTrue(self.manager().sync(["10.0.0.1"])["paused"])
        self.assertFalse(any(act=="DeleteSecurityGroup" for act,args in self.cloud.mutations))

    def test_inventory_any_running_same_vpc_production_subnet_private_v4_only(self):
        base={"VirtualPrivateCloud":{"VpcId":inventory.VPC,"SubnetId":"subnet-future"},
              "Placement":{"Zone":"ap-hongkong-3"},"InstanceState":"RUNNING","SecurityGroupIds":[inventory.SOURCE_GROUP],
              "InstanceName":"ads发布调控-prod-001","InstanceId":"ins-1",
              "PrivateIpAddresses":["10.24.1.5","203.0.113.10","::1","127.0.0.1"]}
        rows=[base,{**base,"InstanceId":"ins-old","InstanceState":"STOPPED"},
              {**base,"InstanceId":"ins-test","InstanceName":"ads发布调控-test-77"},
              {**base,"InstanceId":"ins-other","VirtualPrivateCloud":{"VpcId":"vpc-other"}},
              {**base,"InstanceId":"ins-dev","InstanceName":"ads发布调控临时压测"}]
        actual=inventory.inventory(lambda offset:{"InstanceSet":rows[offset:offset+2],"TotalCount":len(rows)})
        self.assertEqual(actual["addresses"],["10.24.1.5"])
        self.assertEqual(set(actual["instances"]),{"ins-1"})
        with self.assertRaises(ValueError): network.desired_rules(["192.0.2.0/24"])


if __name__ == "__main__":
    unittest.main()
