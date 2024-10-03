from __future__ import annotations

import abc
import json
import os
import re
import subprocess
import tempfile
import textwrap
from itertools import chain, product
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    TypeVar,
    cast,
)

import toml

from fixtures.common_types import Lsn, TenantId, TimelineId
from fixtures.log_helper import log
from fixtures.pageserver.common_types import IndexPartDump
from fixtures.pg_version import PgVersion
from fixtures.utils import AuxFileStore

T = TypeVar("T")


class AbstractNeonCli(abc.ABC):
    """
    A typed wrapper around an arbitrary Neon CLI tool.
    Supports a way to run arbitrary command directly via CLI.
    Do not use directly, use specific subclasses instead.
    """

    def __init__(self, extra_env: Optional[Dict[str, str]], binpath: Path):
        self.extra_env = extra_env
        self.binpath = binpath

    COMMAND: str = cast(str, None)  # To be overwritten by the derived class.

    def raw_cli(
        self,
        arguments: List[str],
        extra_env_vars: Optional[Dict[str, str]] = None,
        check_return_code=True,
        timeout=None,
    ) -> "subprocess.CompletedProcess[str]":
        """
        Run the command with the specified arguments.

        Arguments must be in list form, e.g. ['endpoint', 'create']

        Return both stdout and stderr, which can be accessed as

        >>> result = env.neon_cli.raw_cli(...)
        >>> assert result.stderr == ""
        >>> log.info(result.stdout)

        If `check_return_code`, on non-zero exit code logs failure and raises.
        """

        assert isinstance(arguments, list)
        assert isinstance(self.COMMAND, str)

        command_path = str(self.binpath / self.COMMAND)

        args = [command_path] + arguments
        log.info('Running command "{}"'.format(" ".join(args)))

        env_vars = os.environ.copy()

        # extra env
        for extra_env_key, extra_env_value in (self.extra_env or {}).items():
            env_vars[extra_env_key] = extra_env_value
        for extra_env_key, extra_env_value in (extra_env_vars or {}).items():
            env_vars[extra_env_key] = extra_env_value

        # Pass through coverage settings
        var = "LLVM_PROFILE_FILE"
        val = os.environ.get(var)
        if val:
            env_vars[var] = val

        # Intercept CalledProcessError and print more info
        try:
            res = subprocess.run(
                args,
                env=env_vars,
                check=False,
                universal_newlines=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            if e.stderr:
                stderr = e.stderr.decode(errors="replace")
            else:
                stderr = ""

            if e.stdout:
                stdout = e.stdout.decode(errors="replace")
            else:
                stdout = ""

            log.warn(f"CLI timeout: stderr={stderr}, stdout={stdout}")
            raise

        indent = "  "
        if not res.returncode:
            stripped = res.stdout.strip()
            lines = stripped.splitlines()
            if len(lines) < 2:
                log.debug(f"Run {res.args} success: {stripped}")
            else:
                log.debug("Run %s success:\n%s" % (res.args, textwrap.indent(stripped, indent)))
        elif check_return_code:
            # this way command output will be in recorded and shown in CI in failure message
            indent = indent * 2
            msg = textwrap.dedent(
                """\
            Run %s failed:
              stdout:
            %s
              stderr:
            %s
            """
            )
            msg = msg % (
                res.args,
                textwrap.indent(res.stdout.strip(), indent),
                textwrap.indent(res.stderr.strip(), indent),
            )
            log.info(msg)
            raise RuntimeError(msg) from subprocess.CalledProcessError(
                res.returncode, res.args, res.stdout, res.stderr
            )
        return res


class NeonLocalCli(AbstractNeonCli):
    """
    A typed wrapper around the `neon_local` CLI tool.
    Supports main commands via typed methods and a way to run arbitrary command directly via CLI.
    """

    COMMAND = "neon_local"

    def __init__(
        self,
        extra_env: Optional[Dict[str, str]],
        binpath: Path,
        repo_dir: Path,
        pg_distrib_dir: Path,
    ):
        if extra_env is None:
            env_vars = {}
        else:
            env_vars = extra_env.copy()
        env_vars["NEON_REPO_DIR"] = str(repo_dir)
        env_vars["POSTGRES_DISTRIB_DIR"] = str(pg_distrib_dir)

        super().__init__(env_vars, binpath)

    def raw_cli(self, *args, **kwargs) -> subprocess.CompletedProcess[str]:
        return super().raw_cli(*args, **kwargs)

    def create_tenant(
        self,
        tenant_id: TenantId,
        timeline_id: TimelineId,
        pg_version: PgVersion,
        conf: Optional[Dict[str, Any]] = None,
        shard_count: Optional[int] = None,
        shard_stripe_size: Optional[int] = None,
        placement_policy: Optional[str] = None,
        set_default: bool = False,
        aux_file_policy: Optional[AuxFileStore] = None,
    ):
        """
        Creates a new tenant, returns its id and its initial timeline's id.
        """
        args = [
            "tenant",
            "create",
            "--tenant-id",
            str(tenant_id),
            "--timeline-id",
            str(timeline_id),
            "--pg-version",
            pg_version,
        ]
        if conf is not None:
            args.extend(
                chain.from_iterable(
                    product(["-c"], (f"{key}:{value}" for key, value in conf.items()))
                )
            )

        if aux_file_policy is AuxFileStore.V2:
            args.extend(["-c", "switch_aux_file_policy:v2"])
        elif aux_file_policy is AuxFileStore.V1:
            args.extend(["-c", "switch_aux_file_policy:v1"])
        elif aux_file_policy is AuxFileStore.CrossValidation:
            args.extend(["-c", "switch_aux_file_policy:cross-validation"])

        if set_default:
            args.append("--set-default")

        if shard_count is not None:
            args.extend(["--shard-count", str(shard_count)])

        if shard_stripe_size is not None:
            args.extend(["--shard-stripe-size", str(shard_stripe_size)])

        if placement_policy is not None:
            args.extend(["--placement-policy", str(placement_policy)])

        res = self.raw_cli(args)
        res.check_returncode()

    def import_tenant(self, tenant_id: TenantId):
        args = ["tenant", "import", "--tenant-id", str(tenant_id)]
        res = self.raw_cli(args)
        res.check_returncode()

    def set_default(self, tenant_id: TenantId):
        """
        Update default tenant for future operations that require tenant_id.
        """
        res = self.raw_cli(["tenant", "set-default", "--tenant-id", str(tenant_id)])
        res.check_returncode()

    def config_tenant(self, tenant_id: TenantId, conf: Dict[str, str]):
        """
        Update tenant config.
        """

        args = ["tenant", "config", "--tenant-id", str(tenant_id)]
        if conf is not None:
            args.extend(
                chain.from_iterable(
                    product(["-c"], (f"{key}:{value}" for key, value in conf.items()))
                )
            )

        res = self.raw_cli(args)
        res.check_returncode()

    def list_tenants(self) -> "subprocess.CompletedProcess[str]":
        res = self.raw_cli(["tenant", "list"])
        res.check_returncode()
        return res

    def create_timeline(
        self,
        new_branch_name: str,
        tenant_id: TenantId,
        timeline_id: TimelineId,
        pg_version: PgVersion,
    ) -> TimelineId:
        if timeline_id is None:
            timeline_id = TimelineId.generate()

        cmd = [
            "timeline",
            "create",
            "--branch-name",
            new_branch_name,
            "--tenant-id",
            str(tenant_id),
            "--timeline-id",
            str(timeline_id),
            "--pg-version",
            pg_version,
        ]

        res = self.raw_cli(cmd)
        res.check_returncode()

        return timeline_id

    def create_branch(
        self,
        tenant_id: TenantId,
        timeline_id: TimelineId,
        new_branch_name,
        ancestor_branch_name: Optional[str] = None,
        ancestor_start_lsn: Optional[Lsn] = None,
    ):
        cmd = [
            "timeline",
            "branch",
            "--branch-name",
            new_branch_name,
            "--timeline-id",
            str(timeline_id),
            "--tenant-id",
            str(tenant_id),
        ]
        if ancestor_branch_name is not None:
            cmd.extend(["--ancestor-branch-name", ancestor_branch_name])
        if ancestor_start_lsn is not None:
            cmd.extend(["--ancestor-start-lsn", str(ancestor_start_lsn)])

        res = self.raw_cli(cmd)
        res.check_returncode()

    def timeline_import(
        self,
        tenant_id: TenantId,
        timeline_id: TimelineId,
        new_branch_name: str,
        base_lsn: Lsn,
        base_tarfile: Path,
        pg_version: PgVersion,
        end_lsn: Optional[Lsn] = None,
        wal_tarfile: Optional[Path] = None,
    ):
        cmd = [
            "timeline",
            "import",
            "--tenant-id",
            str(tenant_id),
            "--timeline-id",
            str(timeline_id),
            "--pg-version",
            pg_version,
            "--branch-name",
            new_branch_name,
            "--base-lsn",
            str(base_lsn),
            "--base-tarfile",
            str(base_tarfile),
        ]
        if end_lsn is not None:
            cmd.extend(["--end-lsn", str(end_lsn)])
        if wal_tarfile is not None:
            cmd.extend(["--wal-tarfile", str(wal_tarfile)])

        res = self.raw_cli(cmd)
        res.check_returncode()

    def list_timelines(self, tenant_id: TenantId) -> List[Tuple[str, TimelineId]]:
        """
        Returns a list of (branch_name, timeline_id) tuples out of parsed `neon timeline list` CLI output.
        """

        # main [b49f7954224a0ad25cc0013ea107b54b]
        # ┣━ @0/16B5A50: test_cli_branch_list_main [20f98c79111b9015d84452258b7d5540]
        TIMELINE_DATA_EXTRACTOR: re.Pattern = re.compile(  # type: ignore[type-arg]
            r"\s?(?P<branch_name>[^\s]+)\s\[(?P<timeline_id>[^\]]+)\]", re.MULTILINE
        )
        res = self.raw_cli(["timeline", "list", "--tenant-id", str(tenant_id)])
        timelines_cli = sorted(
            map(
                lambda branch_and_id: (branch_and_id[0], TimelineId(branch_and_id[1])),
                TIMELINE_DATA_EXTRACTOR.findall(res.stdout),
            )
        )
        return timelines_cli

    def init(
        self,
        init_config: Dict[str, Any],
        force: Optional[str] = None,
    ) -> "subprocess.CompletedProcess[str]":
        with tempfile.NamedTemporaryFile(mode="w+") as init_config_tmpfile:
            init_config_tmpfile.write(toml.dumps(init_config))
            init_config_tmpfile.flush()

            cmd = [
                "init",
                f"--config={init_config_tmpfile.name}",
            ]

            if force is not None:
                cmd.extend(["--force", force])

            res = self.raw_cli(cmd)
            res.check_returncode()
        return res

    def storage_controller_start(
        self,
        timeout_in_seconds: Optional[int] = None,
        instance_id: Optional[int] = None,
        base_port: Optional[int] = None,
    ):
        cmd = ["storage_controller", "start"]
        if timeout_in_seconds is not None:
            cmd.append(f"--start-timeout={timeout_in_seconds}s")
        if instance_id is not None:
            cmd.append(f"--instance-id={instance_id}")
        if base_port is not None:
            cmd.append(f"--base-port={base_port}")
        return self.raw_cli(cmd)

    def storage_controller_stop(self, immediate: bool, instance_id: Optional[int] = None):
        cmd = ["storage_controller", "stop"]
        if immediate:
            cmd.extend(["-m", "immediate"])
        if instance_id is not None:
            cmd.append(f"--instance-id={instance_id}")
        return self.raw_cli(cmd)

    def pageserver_start(
        self,
        id: int,
        extra_env_vars: Optional[Dict[str, str]] = None,
        timeout_in_seconds: Optional[int] = None,
    ) -> "subprocess.CompletedProcess[str]":
        start_args = ["pageserver", "start", f"--id={id}"]
        if timeout_in_seconds is not None:
            start_args.append(f"--start-timeout={timeout_in_seconds}s")
        return self.raw_cli(start_args, extra_env_vars=extra_env_vars)

    def pageserver_stop(self, id: int, immediate=False) -> "subprocess.CompletedProcess[str]":
        cmd = ["pageserver", "stop", f"--id={id}"]
        if immediate:
            cmd.extend(["-m", "immediate"])

        log.info(f"Stopping pageserver with {cmd}")
        return self.raw_cli(cmd)

    def safekeeper_start(
        self,
        id: int,
        extra_opts: Optional[List[str]] = None,
        extra_env_vars: Optional[Dict[str, str]] = None,
        timeout_in_seconds: Optional[int] = None,
    ) -> "subprocess.CompletedProcess[str]":
        if extra_opts is not None:
            extra_opts = [f"-e={opt}" for opt in extra_opts]
        else:
            extra_opts = []
        if timeout_in_seconds is not None:
            extra_opts.append(f"--start-timeout={timeout_in_seconds}s")
        return self.raw_cli(
            ["safekeeper", "start", str(id), *extra_opts], extra_env_vars=extra_env_vars
        )

    def safekeeper_stop(
        self, id: Optional[int] = None, immediate=False
    ) -> "subprocess.CompletedProcess[str]":
        args = ["safekeeper", "stop"]
        if id is not None:
            args.append(str(id))
        if immediate:
            args.extend(["-m", "immediate"])
        return self.raw_cli(args)

    def broker_start(
        self, timeout_in_seconds: Optional[int] = None
    ) -> "subprocess.CompletedProcess[str]":
        cmd = ["storage_broker", "start"]
        if timeout_in_seconds is not None:
            cmd.append(f"--start-timeout={timeout_in_seconds}s")
        return self.raw_cli(cmd)

    def broker_stop(self) -> "subprocess.CompletedProcess[str]":
        cmd = ["storage_broker", "stop"]
        return self.raw_cli(cmd)

    def endpoint_create(
        self,
        branch_name: str,
        pg_port: int,
        http_port: int,
        tenant_id: TenantId,
        pg_version: PgVersion,
        endpoint_id: Optional[str] = None,
        hot_standby: bool = False,
        lsn: Optional[Lsn] = None,
        pageserver_id: Optional[int] = None,
        allow_multiple=False,
    ) -> "subprocess.CompletedProcess[str]":
        args = [
            "endpoint",
            "create",
            "--tenant-id",
            str(tenant_id),
            "--branch-name",
            branch_name,
            "--pg-version",
            pg_version,
        ]
        if lsn is not None:
            args.extend(["--lsn", str(lsn)])
        if pg_port is not None:
            args.extend(["--pg-port", str(pg_port)])
        if http_port is not None:
            args.extend(["--http-port", str(http_port)])
        if endpoint_id is not None:
            args.append(endpoint_id)
        if hot_standby:
            args.extend(["--hot-standby", "true"])
        if pageserver_id is not None:
            args.extend(["--pageserver-id", str(pageserver_id)])
        if allow_multiple:
            args.extend(["--allow-multiple"])

        res = self.raw_cli(args)
        res.check_returncode()
        return res

    def endpoint_start(
        self,
        endpoint_id: str,
        safekeepers: Optional[List[int]] = None,
        remote_ext_config: Optional[str] = None,
        pageserver_id: Optional[int] = None,
        allow_multiple=False,
        basebackup_request_tries: Optional[int] = None,
    ) -> "subprocess.CompletedProcess[str]":
        args = [
            "endpoint",
            "start",
        ]
        extra_env_vars = {}
        if basebackup_request_tries is not None:
            extra_env_vars["NEON_COMPUTE_TESTING_BASEBACKUP_TRIES"] = str(basebackup_request_tries)
        if remote_ext_config is not None:
            args.extend(["--remote-ext-config", remote_ext_config])

        if safekeepers is not None:
            args.extend(["--safekeepers", (",".join(map(str, safekeepers)))])
        if endpoint_id is not None:
            args.append(endpoint_id)
        if pageserver_id is not None:
            args.extend(["--pageserver-id", str(pageserver_id)])
        if allow_multiple:
            args.extend(["--allow-multiple"])

        res = self.raw_cli(args, extra_env_vars)
        res.check_returncode()
        return res

    def endpoint_reconfigure(
        self,
        endpoint_id: str,
        tenant_id: Optional[TenantId] = None,
        pageserver_id: Optional[int] = None,
        safekeepers: Optional[List[int]] = None,
        check_return_code=True,
    ) -> "subprocess.CompletedProcess[str]":
        args = ["endpoint", "reconfigure", endpoint_id]
        if tenant_id is not None:
            args.extend(["--tenant-id", str(tenant_id)])
        if pageserver_id is not None:
            args.extend(["--pageserver-id", str(pageserver_id)])
        if safekeepers is not None:
            args.extend(["--safekeepers", (",".join(map(str, safekeepers)))])
        return self.raw_cli(args, check_return_code=check_return_code)

    def endpoint_stop(
        self,
        endpoint_id: str,
        destroy=False,
        check_return_code=True,
        mode: Optional[str] = None,
    ) -> "subprocess.CompletedProcess[str]":
        args = [
            "endpoint",
            "stop",
        ]
        if destroy:
            args.append("--destroy")
        if mode is not None:
            args.append(f"--mode={mode}")
        if endpoint_id is not None:
            args.append(endpoint_id)

        return self.raw_cli(args, check_return_code=check_return_code)

    def map_branch(
        self, name: str, tenant_id: TenantId, timeline_id: TimelineId
    ) -> "subprocess.CompletedProcess[str]":
        """
        Map tenant id and timeline id to a neon_local branch name. They do not have to exist.
        Usually needed when creating branches via PageserverHttpClient and not neon_local.

        After creating a name mapping, you can use EndpointFactory.create_start
        with this registered branch name.
        """
        args = [
            "mappings",
            "map",
            "--branch-name",
            name,
            "--tenant-id",
            str(tenant_id),
            "--timeline-id",
            str(timeline_id),
        ]

        return self.raw_cli(args, check_return_code=True)

    def start(self, check_return_code=True) -> "subprocess.CompletedProcess[str]":
        return self.raw_cli(["start"], check_return_code=check_return_code)

    def stop(self, check_return_code=True) -> "subprocess.CompletedProcess[str]":
        return self.raw_cli(["stop"], check_return_code=check_return_code)


class WalCraft(AbstractNeonCli):
    """
    A typed wrapper around the `wal_craft` CLI tool.
    Supports main commands via typed methods and a way to run arbitrary command directly via CLI.
    """

    COMMAND = "wal_craft"

    def postgres_config(self) -> List[str]:
        res = self.raw_cli(["print-postgres-config"])
        res.check_returncode()
        return res.stdout.split("\n")

    def in_existing(self, type: str, connection: str) -> None:
        res = self.raw_cli(["in-existing", type, connection])
        res.check_returncode()


class Pagectl(AbstractNeonCli):
    """
    A typed wrapper around the `pagectl` utility CLI tool.
    """

    COMMAND = "pagectl"

    def dump_index_part(self, path: Path) -> IndexPartDump:
        res = self.raw_cli(["index-part", "dump", str(path)])
        res.check_returncode()
        parsed = json.loads(res.stdout)
        return IndexPartDump.from_json(parsed)