"""Live installer paths and validation constants."""

import re
from pathlib import Path


CONFIG = Path("/usr/lib/mertensia-installer/install.conf")
STATE_ROOT = Path("/run/mertensia-install-state")
MOUNTPOINT = STATE_ROOT / "target"
MAPPER_NAME = "mertensia-root"
MINIMUM_DISK_BYTES = 24 * 1024**3
# Allow firmware/kernel reservations when comparing installed RAM with MemTotal.
# The tested 3 GiB VM exposes 2.74 GiB; the unsafe 2.5 GiB VM exposes 2.31 GiB.
MEMORY_RESERVE_BYTES = 512 * 1024**2
MINIMUM_MEMORY_BYTES = 3 * 1024**3 - MEMORY_RESERVE_BYTES
RECOMMENDED_MEMORY_BYTES = 4 * 1024**3 - MEMORY_RESERVE_BYTES
RECOVERY_PATTERN = re.compile(r"^[A-Z2-9]{6}(?:-[A-Z2-9]{6}){7}$")
SYS_BLOCK = Path("/sys/class/block")
IMAGE_REPOSITORY = "ghcr.io/mertensiaos/mertensiaos"
BLOCK_FIELDS = "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS,DISK-SEQ"
