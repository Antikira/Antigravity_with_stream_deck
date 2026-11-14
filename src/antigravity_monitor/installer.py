"""Installer and setup helper for Antigravity Stream Deck plugin.

Sets up an isolated uv virtual environment, builds a windowless launcher
executable, and installs the plugin to %APPDATA%/Elgato/StreamDeck/Plugins/.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_LAUNCHER_CS_SOURCE = r"""
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace AntigravityLauncher
{
    internal static class Program
    {
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        private static readonly object LogLock = new object();

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(
            IntPtr hJob,
            int jobObjectInfoClass,
            IntPtr lpJobObjectInfo,
            uint cbJobObjectInfoLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr hObject);

        private static void AppendLog(string logFile, string message)
        {
            try
            {
                string line = string.Format(
                    "[{0:yyyy-MM-dd HH:mm:ss}] {1}{2}",
                    DateTime.Now,
                    message,
                    Environment.NewLine
                );
                lock (LogLock)
                {
                    using (
                        var fs = new FileStream(
                            logFile,
                            FileMode.Append,
                            FileAccess.Write,
                            FileShare.ReadWrite | FileShare.Delete
                        )
                    )
                    using (var writer = new StreamWriter(fs, new UTF8Encoding(false)))
                    {
                        writer.Write(line);
                    }
                }
            }
            catch
            {
            }
        }

        private static IntPtr CreateKillOnCloseJob(string logFile)
        {
            try
            {
                IntPtr hJob = CreateJobObject(IntPtr.Zero, null);
                if (hJob == IntPtr.Zero)
                {
                    return IntPtr.Zero;
                }

                var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
                IntPtr infoPtr = Marshal.AllocHGlobal(length);
                try
                {
                    Marshal.StructureToPtr(info, infoPtr, false);
                    if (
                        !SetInformationJobObject(
                            hJob,
                            JobObjectExtendedLimitInformation,
                            infoPtr,
                            (uint)length
                        )
                    )
                    {
                        AppendLog(
                            logFile,
                            "[WARN] SetInformationJobObject failed: "
                                + Marshal.GetLastWin32Error()
                        );
                        CloseHandle(hJob);
                        return IntPtr.Zero;
                    }
                    return hJob;
                }
                finally
                {
                    Marshal.FreeHGlobal(infoPtr);
                }
            }
            catch (Exception ex)
            {
                AppendLog(logFile, "[WARN] Failed to create Job Object: " + ex.Message);
                return IntPtr.Zero;
            }
        }

        private static string EscapeArgument(string arg)
        {
            if (arg == null)
            {
                return "\"\"";
            }

            var sb = new StringBuilder();
            sb.Append('"');
            int backslashCount = 0;
            foreach (char ch in arg)
            {
                if (ch == '\\')
                {
                    backslashCount++;
                }
                else if (ch == '"')
                {
                    sb.Append('\\', backslashCount * 2 + 1);
                    sb.Append('"');
                    backslashCount = 0;
                }
                else
                {
                    if (backslashCount > 0)
                    {
                        sb.Append('\\', backslashCount);
                        backslashCount = 0;
                    }
                    sb.Append(ch);
                }
            }

            if (backslashCount > 0)
            {
                sb.Append('\\', backslashCount * 2);
            }
            sb.Append('"');
            return sb.ToString();
        }

        private static string ResolveProjectDir(string pluginDir)
        {
            string envDir = Environment.GetEnvironmentVariable("ANTIGRAVITY_PROJECT_DIR");
            if (
                !string.IsNullOrEmpty(envDir)
                && Directory.Exists(Path.Combine(envDir, "src", "antigravity_monitor"))
            )
            {
                return envDir;
            }

            string configFile = Path.Combine(pluginDir, "project_path.txt");
            if (File.Exists(configFile))
            {
                string savedPath = File.ReadAllText(configFile, Encoding.UTF8).Trim();
                if (
                    !string.IsNullOrEmpty(savedPath)
                    && Directory.Exists(Path.Combine(savedPath, "src", "antigravity_monitor"))
                )
                {
                    return savedPath;
                }
            }

            string relativeDir = Path.GetFullPath(Path.Combine(pluginDir, "..", ".."));
            if (Directory.Exists(Path.Combine(relativeDir, "src", "antigravity_monitor")))
            {
                return relativeDir;
            }

            return null;
        }

        private static string ResolvePythonExecutable(string projectDir)
        {
            string envPython = Environment.GetEnvironmentVariable("ANTIGRAVITY_PYTHON");
            if (!string.IsNullOrEmpty(envPython) && File.Exists(envPython))
            {
                return envPython;
            }

            string pythonw = Path.Combine(projectDir, ".venv", "Scripts", "pythonw.exe");
            if (File.Exists(pythonw))
            {
                return pythonw;
            }

            string python = Path.Combine(projectDir, ".venv", "Scripts", "python.exe");
            if (File.Exists(python))
            {
                return python;
            }

            return null;
        }

        [STAThread]
        private static int Main(string[] args)
        {
            string pluginDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(
                Path.DirectorySeparatorChar,
                Path.AltDirectorySeparatorChar
            );
            string logFile = Path.Combine(pluginDir, "plugin.log");

            string projectDir = ResolveProjectDir(pluginDir);
            if (string.IsNullOrEmpty(projectDir))
            {
                AppendLog(logFile, "[ERROR] Could not resolve Antigravity project directory.");
                return 1;
            }

            string pythonExe = ResolvePythonExecutable(projectDir);
            if (string.IsNullOrEmpty(pythonExe))
            {
                AppendLog(
                    logFile,
                    "[ERROR] Isolated Python (.venv\\Scripts\\pythonw.exe) not found in "
                        + projectDir
                );
                return 1;
            }

            var escapedArgs = new List<string>
            {
                EscapeArgument("-u"),
                EscapeArgument("-m"),
                EscapeArgument("src.antigravity_monitor.streamdeck_bridge")
            };
            if (args != null)
            {
                foreach (string arg in args)
                {
                    escapedArgs.Add(EscapeArgument(arg));
                }
            }

            var psi = new ProcessStartInfo
            {
                FileName = pythonExe,
                Arguments = string.Join(" ", escapedArgs.ToArray()),
                WorkingDirectory = projectDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardError = true,
                StandardErrorEncoding = Encoding.UTF8
            };
            psi.EnvironmentVariables["PYTHONPATH"] = projectDir;
            psi.EnvironmentVariables["ANTIGRAVITY_LOG_FILE"] = logFile;

            IntPtr jobHandle = CreateKillOnCloseJob(logFile);
            try
            {
                using (Process proc = Process.Start(psi))
                {
                    if (proc == null)
                    {
                        AppendLog(logFile, "[ERROR] Failed to launch python process.");
                        return 1;
                    }

                    if (jobHandle != IntPtr.Zero)
                    {
                        try
                        {
                            if (!AssignProcessToJobObject(jobHandle, proc.Handle))
                            {
                                AppendLog(
                                    logFile,
                                    "[WARN] AssignProcessToJobObject failed: "
                                        + Marshal.GetLastWin32Error()
                                );
                            }
                        }
                        catch (Exception ex)
                        {
                            AppendLog(
                                logFile,
                                "[WARN] AssignProcessToJobObject exception: " + ex.Message
                            );
                        }
                    }

                    proc.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
                    {
                        if (!string.IsNullOrEmpty(e.Data))
                        {
                            AppendLog(logFile, "[STDERR] " + e.Data);
                        }
                    };
                    proc.BeginErrorReadLine();
                    proc.WaitForExit();

                    if (proc.ExitCode != 0)
                    {
                        AppendLog(
                            logFile,
                            "[ERROR] Python process exited with code " + proc.ExitCode + "."
                        );
                    }
                    return proc.ExitCode;
                }
            }
            catch (Exception ex)
            {
                AppendLog(logFile, "[ERROR] Exception launching python process: " + ex);
                return 1;
            }
            finally
            {
                if (jobHandle != IntPtr.Zero)
                {
                    CloseHandle(jobHandle);
                }
            }
        }
    }
}
"""


def get_streamdeck_plugins_dir() -> Path:
    """Return the Elgato Stream Deck plugins directory on Windows."""
    app_data = os.environ.get("APPDATA")
    if not app_data:
        raise RuntimeError("APPDATA environment variable not found.")
    return Path(app_data) / "Elgato" / "StreamDeck" / "Plugins"


def ensure_uv_available() -> str:
    """Return the uv executable path or raise RuntimeError if uv is missing."""
    uv_bin = shutil.which("uv")
    if not uv_bin:
        raise RuntimeError("uv command not found. Please install uv first.")
    return uv_bin


def get_venv_pythonw(project_root: Path) -> Path:
    """Return the isolated virtual environment windowless Python executable path."""
    if sys.platform == "win32":
        return project_root / ".venv" / "Scripts" / "pythonw.exe"
    win_pythonw = project_root / ".venv" / "Scripts" / "pythonw.exe"
    if win_pythonw.is_file():
        return win_pythonw
    posix_python = project_root / ".venv" / "bin" / "python"
    if posix_python.is_file():
        return posix_python
    return win_pythonw


def is_venv_healthy(project_root: Path) -> bool:
    """Check whether the workspace .venv exists and its Python interpreter runs cleanly."""
    if sys.platform == "win32":
        py_exe = project_root / ".venv" / "Scripts" / "python.exe"
    else:
        py_exe = project_root / ".venv" / "bin" / "python"

    if not py_exe.is_file() or not get_venv_pythonw(project_root).is_file():
        return False

    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        result = subprocess.run(
            [str(py_exe), "-c", "import sys"],
            capture_output=True,
            timeout=5.0,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
        return result.returncode == 0
    except Exception:
        return False


def setup_isolated_venv(project_root: Path) -> Path:
    """Create or repair the workspace .venv via uv without polluting global Python."""
    uv_bin = ensure_uv_available()

    if not is_venv_healthy(project_root):
        print(f"Creating isolated virtual environment in {project_root / '.venv'}...")
        subprocess.run([uv_bin, "venv", "--clear"], cwd=project_root, check=True)

    print("Syncing runtime dependencies via uv...")
    subprocess.run([uv_bin, "sync", "--no-dev"], cwd=project_root, check=True)

    pythonw = get_venv_pythonw(project_root)
    if not pythonw.is_file():
        raise FileNotFoundError(f"Isolated Python executable not found: {pythonw}")

    return pythonw


def find_csc_compiler() -> Path | None:
    """Find the .NET C# compiler (csc.exe) on Windows."""
    which_csc = shutil.which("csc")
    if which_csc:
        csc_path = Path(which_csc)
        if csc_path.is_file():
            return csc_path

    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = [
        windir / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        windir / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def build_windowless_launcher(output_exe: Path) -> Path:
    """Compile a GUI-subsystem Windows launcher executable with no console window."""
    csc = find_csc_compiler()
    if csc is None:
        raise RuntimeError("C# compiler (csc.exe) not found to build windowless launcher.")

    output_exe.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        cs_file = Path(tmpdir) / "launcher.cs"
        cs_file.write_text(_LAUNCHER_CS_SOURCE, encoding="utf-8")
        try:
            subprocess.run(
                [
                    str(csc),
                    "/nologo",
                    "/target:winexe",
                    "/optimize+",
                    f"/out:{output_exe}",
                    str(cs_file),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"Failed to compile windowless launcher: {details}") from exc

    return output_exe


def install_plugin(
    source_dir: Path | None = None,
    symlink: bool = False,
    setup_venv: bool = True,
    build_launcher: bool = True,
) -> Path:
    """Install the plugin to the Stream Deck plugins folder."""
    project_root = Path(__file__).resolve().parent.parent.parent
    if source_dir is None:
        source_dir = project_root / "sdplugin" / "com.user.antigravity.sdPlugin"

    if not source_dir.is_dir():
        raise FileNotFoundError(f"Plugin source directory not found: {source_dir}")

    if setup_venv:
        setup_isolated_venv(project_root)

    target_plugins_dir = get_streamdeck_plugins_dir()
    target_plugins_dir.mkdir(parents=True, exist_ok=True)

    dest_dir = target_plugins_dir / source_dir.name

    if dest_dir.is_symlink():
        dest_dir.unlink()
    elif symlink and dest_dir.exists():
        shutil.rmtree(dest_dir)

    installed_via_symlink = False
    if symlink:
        try:
            print(f"Creating symlink from {source_dir} to {dest_dir}...")
            dest_dir.symlink_to(source_dir, target_is_directory=True)
            print("Successfully installed via symlink!")
            installed_via_symlink = True
        except OSError:
            print("Symlink creation failed. Falling back to directory copy...")

    if not installed_via_symlink:
        print(f"Copying {source_dir} to {dest_dir}...")
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, dest_dir, dirs_exist_ok=True)
        print("Successfully copied plugin!")

    # Save project root directory to project_path.txt for launcher / run.bat
    config_file = dest_dir / "project_path.txt"
    try:
        config_file.write_text(str(project_root), encoding="utf-8")
        print(f"Configured project path: {project_root}")
    except Exception as e:
        print(f"Warning: Failed to write project_path.txt: {e}")

    if build_launcher:
        launcher_exe = build_windowless_launcher(dest_dir / "antigravity_launcher.exe")
        print(f"Built windowless launcher: {launcher_exe}")

    return dest_dir


def uninstall_plugin(plugin_name: str = "com.user.antigravity.sdPlugin") -> bool:
    """Remove plugin from Stream Deck plugins folder."""
    target_plugins_dir = get_streamdeck_plugins_dir()
    dest_dir = target_plugins_dir / plugin_name

    if not dest_dir.exists() and not dest_dir.is_symlink():
        print(f"Plugin not found at {dest_dir}")
        return False

    print(f"Uninstalling {dest_dir}...")
    if dest_dir.is_symlink():
        for artifact_name in ("antigravity_launcher.exe", "project_path.txt"):
            artifact_path = dest_dir / artifact_name
            try:
                if artifact_path.is_file():
                    artifact_path.unlink()
            except OSError:
                pass
        dest_dir.unlink()
    else:
        shutil.rmtree(dest_dir)
    print("Uninstalled successfully.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Antigravity Stream Deck plugin")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall the plugin")
    parser.add_argument("--symlink", action="store_true", help="Try symlinking instead of copying")
    parser.add_argument(
        "--skip-venv",
        action="store_true",
        help="Skip running uv venv/sync during installation",
    )
    args = parser.parse_args()

    if args.uninstall:
        uninstall_plugin()
    else:
        dest = install_plugin(symlink=args.symlink, setup_venv=not args.skip_venv)
        print(f"Plugin installed to: {dest}")
        print("Please restart Elgato Stream Deck software to load the plugin.")


if __name__ == "__main__":
    main()
