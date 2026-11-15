"""
Oracle Cloud auto-retry, GitHub Actions version.
Each run tries a few times (about 4 minutes), then exits.
The workflow runs it every 5 minutes until a VM gets created.
All credentials come from GitHub Secrets (environment variables).
"""

import os
import sys
import time
import oci

# ---------------- SETTINGS ----------------
INSTANCE_NAME = "v2Ray"
SUBNET_NAME = "public subnet-v2ray"
ARM_OCPUS = 1
ARM_MEMORY_GB = 6
TRY_MICRO_TOO = True
TRIES_PER_RUN = 3
SECONDS_BETWEEN_TRIES = 45
# ------------------------------------------

ARM_SHAPE = "VM.Standard.A1.Flex"
MICRO_SHAPE = "VM.Standard.E2.1.Micro"


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def main():
    config = {
        "user": os.environ["OCI_USER"],
        "fingerprint": os.environ["OCI_FINGERPRINT"],
        "tenancy": os.environ["OCI_TENANCY"],
        "region": os.environ["OCI_REGION"],
        "key_content": os.environ["OCI_PRIVATE_KEY"],
    }
    oci.config.validate_config(config)
    tenancy = config["tenancy"]
    ssh_key = os.environ["SSH_PUBLIC_KEY"].strip()

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)

    # Safety: stop if a free VM already exists
    existing = [
        i for i in compute.list_instances(tenancy).data
        if i.lifecycle_state not in ("TERMINATED", "TERMINATING")
        and i.shape in (ARM_SHAPE, MICRO_SHAPE)
    ]
    if existing:
        log(f"A free VM already exists ({existing[0].lifecycle_state}). Nothing to do.")
        set_output("created", "true")
        return 0

    ad = identity.list_availability_domains(tenancy).data[0].name

    subnets = network.list_subnets(tenancy, display_name=SUBNET_NAME).data
    if not subnets:
        log(f"Subnet '{SUBNET_NAME}' not found.")
        return 1
    subnet_id = subnets[0].id

    def ubuntu_image(shape):
        imgs = compute.list_images(
            tenancy,
            operating_system="Canonical Ubuntu",
            operating_system_version="24.04",
            shape=shape,
            sort_by="TIMECREATED",
            sort_order="DESC",
        ).data
        return imgs[0].id if imgs else None

    attempts = [(ARM_SHAPE, ubuntu_image(ARM_SHAPE))]
    if TRY_MICRO_TOO:
        attempts.append((MICRO_SHAPE, ubuntu_image(MICRO_SHAPE)))
    attempts = [(s, i) for s, i in attempts if i]
    if not attempts:
        log("No Ubuntu 24.04 images found.")
        return 1

    def details(shape, image_id):
        d = oci.core.models.LaunchInstanceDetails(
            availability_domain=ad,
            compartment_id=tenancy,
            display_name=INSTANCE_NAME,
            shape=shape,
            source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image_id),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id, assign_public_ip=True
            ),
            metadata={"ssh_authorized_keys": ssh_key},
        )
        if shape == ARM_SHAPE:
            d.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ARM_OCPUS, memory_in_gbs=ARM_MEMORY_GB
            )
        return d

    for n in range(1, TRIES_PER_RUN + 1):
        for shape, image_id in attempts:
            try:
                compute.launch_instance(details(shape, image_id))
                log(f"SUCCESS: created {shape}")
                set_output("created", "true")
                set_output("shape", shape)
                return 0
            except oci.exceptions.ServiceError as e:
                text = (e.message or "").lower()
                if "capacity" in text:
                    log(f"Try {n}: {shape} out of capacity")
                    time.sleep(15)  # small gap so Oracle doesn't rate limit us
                elif e.status == 429:
                    log("Rate limited, waiting 40s")
                    time.sleep(40)
                else:
                    log(f"Unexpected error ({e.status} {e.code}): {e.message}")
                    return 1
        if n < TRIES_PER_RUN:
            time.sleep(SECONDS_BETWEEN_TRIES)

    log("No capacity this run. Next run in ~5 min.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
