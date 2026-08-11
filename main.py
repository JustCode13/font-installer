import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from threading import Thread
from queue import Queue, Empty


# ============================================================
# CONFIGURATION
# ============================================================

SUPPORTED_FONTS = {
    ".ttf",
    ".otf",
    ".ttc",
    ".fon",
}

FONT_REGISTRY_PATHS = [
    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
]

FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


# ============================================================
# ADMINISTRATOR CHECK
# ============================================================

def is_admin():
    """Return True if the program is running as administrator."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def restart_as_admin():
    """
    Restart the current program with Administrator privileges.
    """
    try:
        script = os.path.abspath(sys.argv[0])

        params = " ".join(
            f'"{arg}"' for arg in sys.argv[1:]
        )

        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            f'"{script}" {params}',
            None,
            1,
        )

        if result <= 32:
            raise RuntimeError(
                f"Failed to request administrator privileges. "
                f"Error code: {result}"
            )

        sys.exit(0)

    except Exception as e:
        messagebox.showerror(
            "Administrator Required",
            f"Could not restart as Administrator.\n\n{e}",
        )
        sys.exit(1)


# ============================================================
# FONT DISCOVERY
# ============================================================

def find_fonts(folder):
    """
    Recursively search the selected folder for font files.
    """
    fonts = []

    try:
        root = Path(folder)

        if not root.exists():
            raise FileNotFoundError(
                f"Folder does not exist:\n{folder}"
            )

        if not root.is_dir():
            raise NotADirectoryError(
                f"Not a directory:\n{folder}"
            )

        for path in root.rglob("*"):
            try:
                if path.is_file() and path.suffix.lower() in SUPPORTED_FONTS:
                    fonts.append(path)

            except (PermissionError, OSError):
                # Ignore folders/files that cannot be accessed.
                continue

    except Exception:
        raise

    return sorted(fonts, key=lambda p: str(p).lower())


# ============================================================
# FONT NAME EXTRACTION
# ============================================================

def get_font_name(font_path):
    """
    Try to obtain a readable font name.

    Uses Windows PowerShell/.NET font APIs when possible.
    Falls back to the filename.
    """

    try:
        # Windows .NET PrivateFontCollection can inspect font files.
        ps_script = r'''
Add-Type -AssemblyName System.Drawing

$fontPath = $args[0]

try {
    $collection = New-Object System.Drawing.Text.PrivateFontCollection
    $collection.AddFontFile($fontPath)

    if ($collection.Families.Count -gt 0) {
        Write-Output $collection.Families[0].Name
    }
}
catch {
    exit 1
}
'''

        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_script,
                str(font_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        name = result.stdout.strip()

        if name:
            return name

    except Exception:
        pass

    return font_path.stem


# ============================================================
# WINDOWS FONT INSTALLATION
# ============================================================

def broadcast_font_change():
    """
    Tell Windows that the installed fonts have changed.
    """

    HWND_BROADCAST = 0xFFFF
    WM_FONTCHANGE = 0x001D
    SMTO_ABORTIFHUNG = 0x0002

    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_FONTCHANGE,
            0,
            0,
            SMTO_ABORTIFHUNG,
            1000,
            None,
        )
    except Exception:
        pass


def delete_existing_font_files(font_name):
    """
    Try to remove existing font files that belong to the same
    family/name from Windows Fonts directory.

    This is best-effort because Windows may have the font in use.
    """

    removed = []

    try:
        # Search common font filename variants.
        for existing in FONT_DIR.iterdir():

            if not existing.is_file():
                continue

            if existing.suffix.lower() not in SUPPORTED_FONTS:
                continue

            filename = existing.stem.lower()
            target = font_name.lower()

            # Match exact or obvious family-name relationship.
            if (
                filename == target
                or filename.startswith(target + " ")
                or target.startswith(filename + " ")
            ):
                try:
                    existing.unlink()
                    removed.append(existing)
                except PermissionError:
                    pass
                except OSError:
                    pass

    except Exception:
        pass

    return removed


def install_font(font_path):
    """
    Install a single font into Windows.

    Returns:
        (success, message)
    """

    try:
        if not font_path.exists():
            return False, "File does not exist."

        if not font_path.is_file():
            return False, "Path is not a file."

        if font_path.suffix.lower() not in SUPPORTED_FONTS:
            return False, "Unsupported font format."

        if font_path.stat().st_size == 0:
            return False, "Font file is empty."

    except PermissionError:
        return False, "Permission denied while reading the file."

    except OSError as e:
        return False, f"Could not read file: {e}"

    font_name = get_font_name(font_path)

    # --------------------------------------------------------
    # TEMPORARY COPY
    # --------------------------------------------------------

    temp_dir = None

    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="font_installer_"))

        temp_font = temp_dir / font_path.name

        shutil.copy2(font_path, temp_font)

    except Exception as e:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

        return False, f"Could not prepare font: {e}"

    # --------------------------------------------------------
    # WINDOWS FONT INSTALL API
    # --------------------------------------------------------

    try:
        # AddFontResourceExW loads the font for the system.
        FR_PRIVATE = 0x10
        FR_NOT_ENUM = 0x20

        # First attempt: install normally.
        result = ctypes.windll.gdi32.AddFontResourceExW(
            str(temp_font),
            0,
            0,
        )

        if result == 0:
            # Try Windows Shell font installation as fallback.
            try:
                subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        (
                            "$shell = New-Object -ComObject Shell.Application; "
                            f"$font = Get-Item -LiteralPath '{str(temp_font).replace(chr(39), chr(39)+chr(39))}'; "
                            "$fonts = $shell.Namespace(0x14); "
                            "$fonts.CopyHere($font.FullName, 0x10);"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

            except Exception:
                pass

            # Give Windows a moment to process the installation.
            result2 = ctypes.windll.gdi32.AddFontResourceExW(
                str(temp_font),
                0,
                0,
            )

            if result2 == 0:
                shutil.rmtree(temp_dir, ignore_errors=True)

                return False, (
                    "Windows rejected the font. "
                    "It may be invalid or corrupted."
                )

        # ----------------------------------------------------
        # COPY FONT TO WINDOWS FONT DIRECTORY
        # ----------------------------------------------------

        destination = FONT_DIR / font_path.name

        try:
            # If destination exists, remove it first.
            if destination.exists():
                try:
                    destination.unlink()
                except PermissionError:
                    pass

            shutil.copy2(temp_font, destination)

        except PermissionError:
            # Font may already be registered/locked.
            pass

        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)

            return False, (
                f"Font was loaded but could not be copied "
                f"to Windows Fonts: {e}"
            )

        # ----------------------------------------------------
        # REGISTRY REGISTRATION
        # ----------------------------------------------------

        try:
            import winreg

            registry_name = font_path.stem

            # Add proper suffix for registry.
            if font_path.suffix.lower() == ".ttf":
                registry_name += " (TrueType)"

            elif font_path.suffix.lower() == ".otf":
                registry_name += " (OpenType)"

            elif font_path.suffix.lower() == ".ttc":
                registry_name += " (TrueType Collection)"

            elif font_path.suffix.lower() == ".fon":
                registry_name += " (FON)"

            registry_path = FONT_REGISTRY_PATHS[0]

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                registry_path,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:

                winreg.SetValueEx(
                    key,
                    registry_name,
                    0,
                    winreg.REG_SZ,
                    font_path.name,
                )

        except PermissionError:
            # Font itself may still be installed.
            pass

        except Exception:
            # Registry failure shouldn't stop other fonts.
            pass

        # ----------------------------------------------------
        # CLEANUP
        # ----------------------------------------------------

        shutil.rmtree(temp_dir, ignore_errors=True)

        broadcast_font_change()

        return True, f"Installed successfully as '{font_name}'."

    except PermissionError:
        shutil.rmtree(temp_dir, ignore_errors=True)

        return False, "Permission denied by Windows."

    except OSError as e:
        shutil.rmtree(temp_dir, ignore_errors=True)

        return False, f"Windows error: {e}"

    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)

        return False, f"Unexpected error: {e}"


# ============================================================
# GUI APPLICATION
# ============================================================

class FontInstallerApp:

    def __init__(self, root):
        self.root = root

        self.root.title("Bulk Font Installer")
        self.root.geometry("850x650")
        self.root.minsize(700, 550)

        self.queue = Queue()

        self.running = False
        self.fonts = []

        self.success_count = 0
        self.failed_count = 0

        self.create_gui()

        self.root.after(100, self.process_queue)

    # --------------------------------------------------------
    # GUI
    # --------------------------------------------------------

    def create_gui(self):

        main = ttk.Frame(self.root, padding=15)
        main.pack(fill="both", expand=True)

        # ----------------------------------------------------
        # TITLE
        # ----------------------------------------------------

        title = ttk.Label(
            main,
            text="Bulk Font Installer",
            font=("Segoe UI", 20, "bold"),
        )

        title.pack(anchor="w")

        subtitle = ttk.Label(
            main,
            text=(
                "Select a folder and install every supported font "
                "inside it and its subfolders."
            ),
            font=("Segoe UI", 10),
        )

        subtitle.pack(anchor="w", pady=(3, 15))

        # ----------------------------------------------------
        # FOLDER
        # ----------------------------------------------------

        folder_frame = ttk.LabelFrame(
            main,
            text="Font Folder",
            padding=10,
        )

        folder_frame.pack(fill="x", pady=(0, 10))

        self.folder_var = tk.StringVar()

        self.folder_entry = ttk.Entry(
            folder_frame,
            textvariable=self.folder_var,
        )

        self.folder_entry.pack(
            side="left",
            fill="x",
            expand=True,
        )

        self.browse_button = ttk.Button(
            folder_frame,
            text="Browse...",
            command=self.browse_folder,
        )

        self.browse_button.pack(
            side="left",
            padx=(8, 0),
        )

        # ----------------------------------------------------
        # BUTTONS
        # ----------------------------------------------------

        button_frame = ttk.Frame(main)

        button_frame.pack(fill="x", pady=(0, 10))

        self.scan_button = ttk.Button(
            button_frame,
            text="Scan Fonts",
            command=self.scan_fonts,
        )

        self.scan_button.pack(side="left")

        self.install_button = ttk.Button(
            button_frame,
            text="Install All Fonts",
            command=self.start_installation,
            state="disabled",
        )

        self.install_button.pack(
            side="left",
            padx=(8, 0),
        )

        self.clear_button = ttk.Button(
            button_frame,
            text="Clear Log",
            command=self.clear_log,
        )

        self.clear_button.pack(side="right")

        # ----------------------------------------------------
        # PROGRESS
        # ----------------------------------------------------

        progress_frame = ttk.LabelFrame(
            main,
            text="Progress",
            padding=10,
        )

        progress_frame.pack(fill="x", pady=(0, 10))

        self.progress = ttk.Progressbar(
            progress_frame,
            orient="horizontal",
            mode="determinate",
        )

        self.progress.pack(fill="x")

        self.status_var = tk.StringVar(
            value="Select a folder to begin."
        )

        status = ttk.Label(
            progress_frame,
            textvariable=self.status_var,
        )

        status.pack(
            anchor="w",
            pady=(6, 0),
        )

        # ----------------------------------------------------
        # COUNTERS
        # ----------------------------------------------------

        counter_frame = ttk.Frame(main)

        counter_frame.pack(
            fill="x",
            pady=(0, 10),
        )

        self.found_var = tk.StringVar(value="Found: 0")
        self.success_var = tk.StringVar(value="Installed: 0")
        self.failed_var = tk.StringVar(value="Failed: 0")

        ttk.Label(
            counter_frame,
            textvariable=self.found_var,
        ).pack(side="left", padx=(0, 20))

        ttk.Label(
            counter_frame,
            textvariable=self.success_var,
        ).pack(side="left", padx=(0, 20))

        ttk.Label(
            counter_frame,
            textvariable=self.failed_var,
        ).pack(side="left")

        # ----------------------------------------------------
        # LOG
        # ----------------------------------------------------

        log_frame = ttk.LabelFrame(
            main,
            text="Installation Log",
            padding=5,
        )

        log_frame.pack(
            fill="both",
            expand=True,
        )

        self.log = tk.Text(
            log_frame,
            wrap="word",
            font=("Consolas", 10),
            state="disabled",
        )

        scrollbar = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log.yview,
        )

        self.log.configure(
            yscrollcommand=scrollbar.set
        )

        self.log.pack(
            side="left",
            fill="both",
            expand=True,
        )

        scrollbar.pack(
            side="right",
            fill="y",
        )

    # --------------------------------------------------------
    # LOGGING
    # --------------------------------------------------------

    def write_log(self, text):

        self.log.configure(state="normal")

        self.log.insert(
            "end",
            text + "\n",
        )

        self.log.see("end")

        self.log.configure(state="disabled")

    def clear_log(self):

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # --------------------------------------------------------
    # FOLDER
    # --------------------------------------------------------

    def browse_folder(self):

        folder = filedialog.askdirectory(
            title="Select folder containing fonts"
        )

        if folder:
            self.folder_var.set(folder)

            self.scan_fonts()

    # --------------------------------------------------------
    # SCAN
    # --------------------------------------------------------

    def scan_fonts(self):

        if self.running:
            return

        folder = self.folder_var.get().strip()

        if not folder:
            messagebox.showwarning(
                "No Folder",
                "Please select a folder first.",
            )
            return

        self.status_var.set("Scanning for fonts...")
        self.install_button.config(state="disabled")

        self.root.update_idletasks()

        try:
            self.fonts = find_fonts(folder)

        except Exception as e:

            messagebox.showerror(
                "Scan Error",
                str(e),
            )

            self.status_var.set(
                "Could not scan the selected folder."
            )

            return

        count = len(self.fonts)

        self.found_var.set(
            f"Found: {count}"
        )

        if count == 0:

            self.status_var.set(
                "No supported font files were found."
            )

            messagebox.showinfo(
                "No Fonts Found",
                (
                    "No supported font files were found.\n\n"
                    "Supported formats:\n"
                    ".ttf\n"
                    ".otf\n"
                    ".ttc\n"
                    ".fon"
                ),
            )

            return

        self.status_var.set(
            f"Found {count} font file(s). Ready to install."
        )

        self.write_log(
            f"Found {count} font file(s)."
        )

        self.write_log(
            f"Folder: {folder}"
        )

        self.write_log(
            "-" * 70
        )

        self.install_button.config(
            state="normal"
        )

    # --------------------------------------------------------
    # INSTALLATION
    # --------------------------------------------------------

    def start_installation(self):

        if self.running:
            return

        if not self.fonts:

            messagebox.showwarning(
                "No Fonts",
                "Scan a folder first.",
            )

            return

        answer = messagebox.askyesno(
            "Install Fonts",
            (
                f"Install {len(self.fonts)} font file(s)?\n\n"
                "Existing fonts will be replaced/updated "
                "where Windows allows it."
            ),
        )

        if not answer:
            return

        self.running = True

        self.success_count = 0
        self.failed_count = 0

        self.success_var.set("Installed: 0")
        self.failed_var.set("Failed: 0")

        self.progress["maximum"] = len(self.fonts)
        self.progress["value"] = 0

        self.scan_button.config(state="disabled")
        self.install_button.config(state="disabled")
        self.browse_button.config(state="disabled")

        self.status_var.set("Installing fonts...")

        self.write_log("")
        self.write_log("=" * 70)
        self.write_log("STARTING FONT INSTALLATION")
        self.write_log("=" * 70)

        thread = Thread(
            target=self.install_worker,
            daemon=True,
        )

        thread.start()

    # --------------------------------------------------------
    # WORKER THREAD
    # --------------------------------------------------------

    def install_worker(self):

        total = len(self.fonts)

        for index, font_path in enumerate(
            self.fonts,
            start=1,
        ):

            self.queue.put(
                (
                    "status",
                    f"Installing {index}/{total}: "
                    f"{font_path.name}",
                )
            )

            success, message = install_font(
                font_path
            )

            if success:

                self.queue.put(
                    (
                        "success",
                        font_path,
                        message,
                    )
                )

            else:

                self.queue.put(
                    (
                        "failed",
                        font_path,
                        message,
                    )
                )

        self.queue.put(
            (
                "finished",
            )
        )

    # --------------------------------------------------------
    # QUEUE PROCESSOR
    # --------------------------------------------------------

    def process_queue(self):

        try:

            while True:

                item = self.queue.get_nowait()

                event = item[0]

                if event == "status":

                    self.status_var.set(
                        item[1]
                    )

                elif event == "success":

                    self.success_count += 1

                    font_path = item[1]
                    message = item[2]

                    self.success_var.set(
                        f"Installed: {self.success_count}"
                    )

                    self.progress["value"] += 1

                    self.write_log(
                        f"[OK] {font_path.name}"
                    )

                    self.write_log(
                        f"     {message}"
                    )

                elif event == "failed":

                    self.failed_count += 1

                    font_path = item[1]
                    message = item[2]

                    self.failed_var.set(
                        f"Failed: {self.failed_count}"
                    )

                    self.progress["value"] += 1

                    self.write_log(
                        f"[FAILED] {font_path.name}"
                    )

                    self.write_log(
                        f"         {message}"
                    )

                elif event == "finished":

                    self.installation_finished()

        except Empty:
            pass

        self.root.after(
            100,
            self.process_queue,
        )

    # --------------------------------------------------------
    # FINISHED
    # --------------------------------------------------------

    def installation_finished(self):

        self.running = False

        self.scan_button.config(
            state="normal"
        )

        self.browse_button.config(
            state="normal"
        )

        self.install_button.config(
            state="normal"
        )

        total = len(self.fonts)

        self.status_var.set(
            f"Finished. Installed {self.success_count} "
            f"of {total} font file(s)."
        )

        self.write_log("")
        self.write_log("=" * 70)
        self.write_log("INSTALLATION FINISHED")
        self.write_log("=" * 70)
        self.write_log(
            f"Total:     {total}"
        )
        self.write_log(
            f"Installed: {self.success_count}"
        )
        self.write_log(
            f"Failed:    {self.failed_count}"
        )
        self.write_log("=" * 70)

        if self.failed_count == 0:

            messagebox.showinfo(
                "Complete",
                (
                    f"All {total} font file(s) "
                    "were installed successfully."
                ),
            )

        else:

            messagebox.showwarning(
                "Completed With Errors",
                (
                    f"Installation finished.\n\n"
                    f"Total: {total}\n"
                    f"Installed: {self.success_count}\n"
                    f"Failed: {self.failed_count}\n\n"
                    "Check the installation log for details."
                ),
            )


# ============================================================
# MAIN
# ============================================================

def main():

    # Make sure we're running on Windows.
    if sys.platform != "win32":

        print(
            "This program is designed for Windows."
        )

        return

    # Request administrator privileges.
    if not is_admin():

        root = tk.Tk()
        root.withdraw()

        answer = messagebox.askyesno(
            "Administrator Permission",
            (
                "Font installation requires Administrator "
                "permission.\n\n"
                "Restart this program as Administrator?"
            ),
        )

        root.destroy()

        if answer:
            restart_as_admin()

        return

    # Start GUI.
    root = tk.Tk()

    # Windows DPI scaling.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = FontInstallerApp(root)

    root.mainloop()


if __name__ == "__main__":
    main()
