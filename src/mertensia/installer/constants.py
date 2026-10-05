"""Live installer paths and validation constants."""

import re
from pathlib import Path


CONFIG = Path("/usr/lib/mertensia-installer/install.conf")
STATE_ROOT = Path("/run/mertensia-install-state")
MOUNTPOINT = STATE_ROOT / "target"
MAPPER_NAME = "mertensia-root"
MINIMUM_DISK_BYTES = 24 * 1024**3
RECOVERY_PATTERN = re.compile(r"^[A-Z2-9]{6}(?:-[A-Z2-9]{6}){7}$")
SYS_BLOCK = Path("/sys/class/block")
IMAGE_REPOSITORY = "ghcr.io/mertensiaos/mertensiaos"
BLOCK_FIELDS = "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS"
