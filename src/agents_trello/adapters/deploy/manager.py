"""Feature branch deployment: Docker + Traefik + single ngrok tunnel."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TRAEFIK_NETWORK = "web"
TRAEFIK_PORT = 80


@dataclass(frozen=True)
class DeployResult:
    success: bool
    deploy_url: str = ""
    container_name: str = ""
    error: str = ""


class DeployManager:
    """Manages local Docker deployments behind Traefik with a single ngrok tunnel.

    Architecture:
      ngrok (one tunnel) -> Traefik :80 -> containers (path-routed)

    Each branch gets a path prefix:
      /main/   -> main container
      /feat-42-add-foo/ -> feature branch container

    All accessible via one ngrok URL.
    """

    def __init__(
        self,
        github_repo: str,
        project_name: str = "app",
        registry: str = "ghcr.io",
        container_port: int = 8000,
        github_token: str = "",
        github_user: str = "",
        state_dir: str = "",
    ) -> None:
        self._github_repo = github_repo
        self._project = project_name
        self._registry = registry
        self._container_port = container_port
        self._github_token = github_token
        self._github_user = github_user or github_repo.split("/")[0]
        # branch -> (container_name, image_digest) — persisted to disk
        self._state_file = Path(state_dir or ".") / "data" / "deploy_state.json"
        self._deployed: dict[str, tuple[str, str]] = self._load_state()
        # Single ngrok tunnel
        self._ngrok_proc: asyncio.subprocess.Process | None = None
        self._ngrok_url: str = ""
        self._logged_in = False

    def _load_state(self) -> dict[str, tuple[str, str]]:
        try:
            data = json.loads(self._state_file.read_text())
            return {k: tuple(v) for k, v in data.items()}  # type: ignore[misc]
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return {}

    def _save_state(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(self._deployed))

    def _image_tag(self, branch: str) -> str:
        safe = branch.replace("/", "-")
        return f"{self._registry}/{self._github_repo}:{safe}"

    def _container_name(self, branch: str) -> str:
        safe = branch.replace("/", "-")
        return f"{self._project}-{safe}"

    def _path_prefix(self, branch: str) -> str:
        safe = branch.replace("/", "-")
        return f"/{safe}"

    async def _run(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(), err.decode()

    async def _ensure_docker_login(self) -> None:
        if self._logged_in or not self._github_token:
            return
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "login",
            self._registry,
            "-u",
            self._github_user,
            "--password-stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate(input=self._github_token.encode())
        if proc.returncode == 0:
            logger.info("docker_login_success", extra={"registry": self._registry})
            self._logged_in = True
        else:
            logger.warning("docker_login_failed", extra={"registry": self._registry})

    async def _get_remote_digest(self, image: str) -> str | None:
        await self._ensure_docker_login()
        rc, out, _ = await self._run("docker", "manifest", "inspect", image, "--verbose")
        if rc != 0:
            return None
        try:
            data = json.loads(out)
            if isinstance(data, list):
                data = data[0]
            return data.get("Descriptor", {}).get("digest", "")
        except (json.JSONDecodeError, KeyError):
            import hashlib

            return hashlib.sha256(out.encode()).hexdigest()

    async def ensure_traefik(self) -> None:
        """Make sure the Traefik container and network are running."""
        # Create network if missing
        await self._run("docker", "network", "create", TRAEFIK_NETWORK)

        # Check if traefik is running
        rc, _, _ = await self._run("docker", "inspect", "traefik")
        if rc != 0:
            logger.info("Starting Traefik")
            await self._run(
                "docker",
                "run",
                "-d",
                "--name",
                "traefik",
                "--network",
                TRAEFIK_NETWORK,
                "--restart",
                "unless-stopped",
                "-p",
                f"{TRAEFIK_PORT}:80",
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock:ro",
                "traefik:v3",
                "--providers.docker=true",
                "--providers.docker.exposedbydefault=false",
                "--providers.docker.network=web",
                "--entrypoints.web.address=:80",
            )

    async def ensure_ngrok(self) -> str:
        """Ensure ngrok is tunneling to Traefik. Returns the public URL."""
        if self._ngrok_url:
            # Check if still alive
            rc, out, _ = await self._run(
                "curl",
                "-s",
                "http://localhost:4040/api/tunnels",
            )
            if rc == 0 and out.strip():
                try:
                    tunnels = json.loads(out).get("tunnels", [])
                    if tunnels:
                        for t in tunnels:
                            if t.get("proto") == "https":
                                self._ngrok_url = t["public_url"]
                                return self._ngrok_url
                        self._ngrok_url = tunnels[0]["public_url"]
                        return self._ngrok_url
                except (json.JSONDecodeError, KeyError):
                    logger.debug("ngrok_api_parse_failed", exc_info=True)

        # Start ngrok pointing at Traefik
        if self._ngrok_proc and self._ngrok_proc.returncode is None:
            self._ngrok_proc.kill()
            await self._ngrok_proc.wait()

        self._ngrok_proc = await asyncio.create_subprocess_exec(
            "ngrok",
            "http",
            str(TRAEFIK_PORT),
            "--log=stdout",
            "--log-format=json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        for _ in range(30):
            await asyncio.sleep(1)
            try:
                rc, out, _ = await self._run(
                    "curl",
                    "-s",
                    "http://localhost:4040/api/tunnels",
                )
                if rc == 0 and out.strip():
                    tunnels = json.loads(out).get("tunnels", [])
                    for t in tunnels:
                        if t.get("proto") == "https":
                            self._ngrok_url = t["public_url"]
                            return self._ngrok_url
                    if tunnels:
                        self._ngrok_url = tunnels[0]["public_url"]
                        return self._ngrok_url
            except Exception:
                logger.debug("ngrok_poll_attempt_failed", exc_info=True)
                continue

        raise RuntimeError("ngrok failed to start within 30s")

    async def check_and_deploy(self, branch: str) -> DeployResult | None:
        """Check if a branch has a new Docker image. Deploy/redeploy if so."""
        image = self._image_tag(branch)
        container = self._container_name(branch)

        remote_digest = await self._get_remote_digest(image)
        if remote_digest is None:
            return None  # No image yet

        existing = self._deployed.get(branch)
        if existing and existing[1] == remote_digest:
            return None  # Already running latest

        logger.info("deploy_new_image", extra={"branch": branch, "image": image})

        # Pull (use linux/amd64 for CI-built images on ARM Macs)
        rc, _, err = await self._run("docker", "pull", "--platform", "linux/amd64", image)
        if rc != 0:
            return DeployResult(success=False, error=f"Pull failed: {err}")

        # Ensure infra
        await self.ensure_traefik()

        # Stop old container
        await self._run("docker", "rm", "-f", container)

        # Start container with Traefik labels for path-based routing
        path = self._path_prefix(branch)
        rc, _, err = await self._run(
            "docker",
            "run",
            "-d",
            "--platform",
            "linux/amd64",
            "--name",
            container,
            "--network",
            TRAEFIK_NETWORK,
            "--restart",
            "unless-stopped",
            "--memory",
            "512m",
            "--cpus",
            "1.0",
            "--label",
            "traefik.enable=true",
            "--label",
            f"traefik.http.routers.{container}.rule=PathPrefix(`{path}`)",
            "--label",
            f"traefik.http.routers.{container}.entrypoints=web",
            "--label",
            (f"traefik.http.middlewares.{container}-strip.stripprefix.prefixes={path}"),
            "--label",
            (f"traefik.http.routers.{container}.middlewares={container}-strip"),
            "--label",
            (f"traefik.http.services.{container}.loadbalancer.server.port={self._container_port}"),
            image,
        )
        if rc != 0:
            return DeployResult(success=False, error=f"Run failed: {err}")

        self._deployed[branch] = (container, remote_digest)
        self._save_state()

        # Ensure ngrok tunnel
        try:
            base_url = await self.ensure_ngrok()
        except RuntimeError as exc:
            return DeployResult(success=False, error=str(exc))

        deploy_url = f"{base_url}{path}"
        logger.info("deploy_success", extra={"branch": branch, "url": deploy_url})
        return DeployResult(success=True, deploy_url=deploy_url, container_name=container)

    async def teardown(self, branch: str) -> None:
        existing = self._deployed.pop(branch, None)
        if existing is None:
            return
        self._save_state()
        await self._run("docker", "rm", "-f", existing[0])
        logger.info("teardown_done", extra={"container": existing[0]})

    def get_deploy_url(self, branch: str) -> str | None:
        if branch in self._deployed and self._ngrok_url:
            return f"{self._ngrok_url}{self._path_prefix(branch)}"
        return None

    async def close(self) -> None:
        if self._ngrok_proc and self._ngrok_proc.returncode is None:
            self._ngrok_proc.kill()
            await self._ngrok_proc.wait()
        for branch in list(self._deployed):
            await self.teardown(branch)
