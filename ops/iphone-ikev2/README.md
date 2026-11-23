# iPhone native IKEv2

Adds independent, removable iOS IKEv2 configurations for CPU 43.166.187.96 and
Hong Kong GPU 43.154.250.89. Uses strongSwan 6.0.6 from EPEL 8, IKEv2 only,
AES-256/SHA-256/MODP2048, a separately generated 256-bit PSK per server,
MOBIKE, UDP encapsulation and IPv4 internet routing. No App Store client,
device supervision, MDM enrollment or root certificate installation is needed.

Runtime JSON contains `slug`, `name`, `public_ip`, `client_id`, `pool`,
`interface` and `psk`. Store it root-only outside Git. iOS profiles contain the
PSKs and must remain private. Do not put profiles or runtime JSON in Git.

Fetch the reviewed GitHub commit, stage the signed RPM, then run:

```sh
python3 ops/iphone-ikev2/deploy.py install --root RUNTIME_ROOT --config RUNTIME_ROOT/identity.json --rpm RPM_PATH
```

The deployment records changed-file backups, original forwarding, package logs
and its exact source commit under `RUNTIME_ROOT/deployments/TIMESTAMP`.
Only strongswan-starter and its own network-rule service are started.
The existing Xray, application, publishing and GPU services are not restarted.
No iptables chains are flushed and only the tagged pool rules are added.
Cloud firewalls must allow inbound UDP 500 and 4500 for the selected server.

Rollback with the same source version:

```sh
python3 ops/iphone-ikev2/deploy.py rollback --deployment RUNTIME_ROOT/deployments/TIMESTAMP
```

Rollback restores changed files and the pre-deployment forwarding value, stops
only this new VPN, and retains the installed package, identities and receipts.
If another operator changed the managed files, reconcile those changes first.

Generate one combined mobileconfig with `profiles.py --config CPU_JSON
--config HK_JSON --output PRIVATE_FILE.mobileconfig`. USB import uses
`pymobiledevice3 profile install PRIVATE_FILE.mobileconfig`; the user must
approve the profile on the phone. Do not supervise or erase the device.
Normally only one of the two personal VPNs is enabled at a time. A running
server or successful profile import alone does not prove mobile connectivity;
confirm an IKE/CHILD SA and the selected public egress IP from the phone.

References: https://docs.strongswan.org/docs/5.9/interop/appleIkev2Profile.html
