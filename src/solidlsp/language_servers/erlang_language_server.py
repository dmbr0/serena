"""Erlang Language Server implementation using Erlang LS."""

import logging
import os
import shutil
import subprocess
import threading
import time

import psutil
from overrides import override

from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.ls_logger import LanguageServerLogger
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo
from solidlsp.settings import SolidLSPSettings


class ErlangLanguageServer(SolidLanguageServer):
    """Language server for Erlang using Erlang LS."""

    def __init__(
        self,
        config: LanguageServerConfig,
        logger: LanguageServerLogger,
        repository_root_path: str,
        solidlsp_settings: SolidLSPSettings,
    ):
        """
        Creates an ErlangLanguageServer instance. This class is not meant to be instantiated directly.
        Use LanguageServer.create() instead.
        """
        self.erlang_ls_path = shutil.which("erlang_ls")
        if not self.erlang_ls_path:
            raise RuntimeError("Erlang LS not found. Install from: https://github.com/erlang-ls/erlang_ls")

        if not self._check_erlang_installation():
            raise RuntimeError("Erlang/OTP not found. Install from: https://www.erlang.org/downloads")

        # Configure Erlang LS command with environment-specific options
        erlang_ls_cmd = [self.erlang_ls_path, "--transport", "stdio"]

        # Add additional flags for CI environments to improve stability
        is_ci = os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true"
        if is_ci:
            # Use more conservative settings in CI
            erlang_ls_cmd.extend(["--log-level", "info"])

        super().__init__(
            config,
            logger,
            repository_root_path,
            ProcessLaunchInfo(cmd=erlang_ls_cmd, cwd=repository_root_path),
            "erlang",
            solidlsp_settings,
        )

        # Add server readiness tracking like Elixir
        self.server_ready = threading.Event()

        # Set very aggressive timeout for Erlang LS in CI due to severe instability
        is_ci = os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true"
        if is_ci:
            # Use very short timeout in CI to prevent hangs - better to fail fast than hang
            request_timeout = 45.0  # 45 seconds max for any single request
        else:
            request_timeout = 60.0  # 1 minute for local
        self.set_request_timeout(request_timeout)

        # Track request timing for circuit breaker
        self._request_count = 0
        self._timeout_count = 0
        self._last_timeout_time = 0

    def _check_erlang_installation(self) -> bool:
        """Check if Erlang/OTP is available."""
        try:
            result = subprocess.run(["erl", "-version"], check=False, capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def _force_kill_if_stuck(self):
        """Force kill the Erlang LS process if it appears stuck."""
        try:
            if hasattr(self.server, "process") and self.server.process:
                pid = self.server.process.pid
                if psutil.pid_exists(pid):
                    process = psutil.Process(pid)
                    # Check if process is consuming CPU or just hung
                    cpu_percent = process.cpu_percent(interval=0.1)
                    self.logger.log(f"Erlang LS process {pid} CPU usage: {cpu_percent}%", logging.INFO)

                    # If CPU is very low, the process might be deadlocked
                    if cpu_percent < 1.0:
                        self.logger.log(f"Force killing potentially deadlocked Erlang LS process {pid}", logging.WARNING)
                        try:
                            process.terminate()
                            time.sleep(2)
                            if process.is_running():
                                process.kill()
                        except psutil.NoSuchProcess:
                            pass
        except Exception as e:
            self.logger.log(f"Error in force kill check: {e}", logging.DEBUG)

    @classmethod
    def _get_erlang_version(cls):
        """Get the installed Erlang/OTP version or None if not found."""
        try:
            result = subprocess.run(["erl", "-version"], check=False, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return result.stderr.strip()  # erl -version outputs to stderr
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
        return None

    @classmethod
    def _check_rebar3_available(cls) -> bool:
        """Check if rebar3 build tool is available."""
        try:
            result = subprocess.run(["rebar3", "version"], check=False, capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def _start_server(self):
        """Start Erlang LS server process with proper initialization waiting."""

        def register_capability_handler(params):
            return

        def window_log_message(msg):
            """Handle window/logMessage notifications from Erlang LS"""
            message_text = msg.get("message", "")
            self.logger.log(f"LSP: window/logMessage: {message_text}", logging.INFO)

            # Look for Erlang LS readiness signals
            # Common patterns: "Started Erlang LS", "initialized", "ready"
            readiness_signals = [
                "Started Erlang LS",
                "server started",
                "initialized",
                "ready to serve requests",
                "compilation finished",
                "indexing complete",
            ]

            message_lower = message_text.lower()
            for signal in readiness_signals:
                if signal.lower() in message_lower:
                    self.logger.log(f"Erlang LS readiness signal detected: {message_text}", logging.INFO)
                    self.server_ready.set()
                    break

            # Log errors that might indicate issues
            if any(word in message_lower for word in ["error", "failed", "timeout", "crash"]):
                self.logger.log(f"Erlang LS potential issue: {message_text}", logging.WARNING)

        def do_nothing(params):
            return

        def check_server_ready(params):
            """Handle $/progress notifications from Erlang LS as fallback."""
            value = params.get("value", {})

            # Check for initialization completion progress
            if value.get("kind") == "end":
                message = value.get("message", "")
                if any(word in message.lower() for word in ["initialized", "ready", "complete"]):
                    self.logger.log("Erlang LS initialization progress completed", logging.INFO)
                    # Set as fallback if no window/logMessage was received
                    if not self.server_ready.is_set():
                        self.server_ready.set()

        # Set up notification handlers
        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", check_server_ready)
        self.server.on_notification("window/workDoneProgress/create", do_nothing)
        self.server.on_notification("$/workDoneProgress", do_nothing)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        self.logger.log("Starting Erlang LS server process", logging.INFO)
        self.server.start()

        # Send initialize request with more robust error handling
        initialize_params = {
            "processId": None,
            "rootPath": self.repository_root_path,
            "rootUri": f"file://{self.repository_root_path}",
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "completion": {"dynamicRegistration": True},
                    "definition": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True},
                    "documentSymbol": {"dynamicRegistration": True},
                    "hover": {"dynamicRegistration": True},
                }
            },
        }

        self.logger.log("Sending initialize request to Erlang LS", logging.INFO)
        try:
            init_response = self.server.send.initialize(initialize_params)

            # Verify server capabilities
            if "capabilities" in init_response:
                self.logger.log(f"Erlang LS capabilities: {list(init_response['capabilities'].keys())}", logging.INFO)

            self.server.notify.initialized({})
            self.completions_available.set()
            self.logger.log("Erlang LS initialization completed successfully", logging.INFO)
        except Exception as e:
            self.logger.log(f"Erlang LS initialization failed: {e}", logging.ERROR)
            # Still set as ready but log the issue
            self.completions_available.set()
            raise

        # Wait for Erlang LS to be ready - adjust timeout based on environment
        is_ci = os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true"

        is_macos = os.uname().sysname == "Darwin" if hasattr(os, "uname") else False

        # macOS in CI can be particularly slow for language server startup
        if is_ci and is_macos:
            ready_timeout = 240.0  # 4 minutes for macOS CI
            env_desc = "macOS CI"
        elif is_ci:
            ready_timeout = 180.0  # 3 minutes for other CI
            env_desc = "CI"
        else:
            ready_timeout = 60.0  # 1 minute for local
            env_desc = "local"

        self.logger.log(f"Waiting up to {ready_timeout} seconds for Erlang LS readiness ({env_desc} environment)...", logging.INFO)

        if self.server_ready.wait(timeout=ready_timeout):
            self.logger.log("Erlang LS is ready and available for requests", logging.INFO)

            # Add settling period for indexing - adjust based on environment
            settling_time = 15.0 if is_ci else 5.0
            self.logger.log(f"Allowing {settling_time} seconds for Erlang LS indexing to complete...", logging.INFO)
            time.sleep(settling_time)
            self.logger.log("Erlang LS settling period complete", logging.INFO)
        else:
            # Set ready anyway and continue - Erlang LS might not send explicit ready messages
            self.logger.log(
                f"Erlang LS readiness timeout reached after {ready_timeout}s, proceeding anyway (common in CI)", logging.WARNING
            )
            self.server_ready.set()

            # Still give some time for basic initialization even without explicit readiness signal
            basic_settling_time = 20.0 if is_ci else 10.0
            self.logger.log(f"Allowing {basic_settling_time} seconds for basic Erlang LS initialization...", logging.INFO)
            time.sleep(basic_settling_time)
            self.logger.log("Basic Erlang LS initialization period complete", logging.INFO)

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        # For Erlang projects, we should ignore:
        # - _build: rebar3 build artifacts
        # - deps: dependencies
        # - ebin: compiled beam files
        # - .rebar3: rebar3 cache
        # - logs: log files
        # - node_modules: if the project has JavaScript components
        return super().is_ignored_dirname(dirname) or dirname in [
            "_build",
            "deps",
            "ebin",
            ".rebar3",
            "logs",
            "node_modules",
            "_checkouts",
            "cover",
        ]

    def is_ignored_filename(self, filename: str) -> bool:
        """Check if a filename should be ignored."""
        # Ignore compiled BEAM files
        if filename.endswith(".beam"):
            return True
        # Don't ignore Erlang source files, header files, or configuration files
        return False

    def request_containing_symbol(self, file_path, line_number, column_number, include_body=False):
        """Override to add timeout logging."""
        start_time = time.time()
        try:
            result = super().request_containing_symbol(file_path, line_number, column_number, include_body)
            elapsed = time.time() - start_time
            if elapsed > 10:  # Log slow requests
                self.logger.log(f"Slow Erlang LS containing symbol request: {elapsed:.1f}s", logging.INFO)
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            self.logger.log(f"Erlang LS containing symbol request failed after {elapsed:.1f}s: {e}", logging.WARNING)
            raise

    def request_referencing_symbols(self, file_path, line_number, column_number):
        """Override to add timeout logging."""
        start_time = time.time()
        try:
            result = super().request_referencing_symbols(file_path, line_number, column_number)
            elapsed = time.time() - start_time
            if elapsed > 10:  # Log slow requests
                self.logger.log(f"Slow Erlang LS referencing symbols request: {elapsed:.1f}s", logging.INFO)
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            self.logger.log(f"Erlang LS referencing symbols request failed after {elapsed:.1f}s: {e}", logging.WARNING)
            raise
