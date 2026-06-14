import numpy as np
import pyaudiowpatch as pyaudio
import pygame
import win32api
import win32con
import win32gui
import math
import sys
import threading
import tkinter as tk
from tkinter import ttk
import pystray
from PIL import Image, ImageDraw

# --- GLOBAL AUDIO-VISUAL SYSTEM CONFIGS ---
BASE_RADIUS = 75          
MAX_THICKNESS = 16        
SPAN_ANGLE = 115          
COLOR_ALERT = (245, 30, 30)
CLEAN_BACKGROUND_KEY = (0, 0, 0)
STEREO_BALANCE_THRESHOLD = 0.20  

# --- PRESET TUNING MATRIX ---
GAME_PROFILES = {
    "Default / Reset": [130, 2200, 70, 5, 0.055],
    "Call of Duty: Warzone": [100, 1800, 85, 4, 0.045],  
    "Fortnite": [150, 2500, 70, 7, 0.060],              
    "Apex Legends": [120, 2000, 75, 5, 0.050],          
    "Valorant / CS": [200, 1500, 90, 3, 0.035]           
}

# Threading & UI Control Signals
overlay_running = False
pygame_thread = None
tray_icon = None
root_window = None

def make_window_click_through(hwnd, rect):
    x, y, right, bottom = rect
    width, height = right - x, bottom - y
    styles = (
        win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT |
        win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW
    )
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, styles)
    win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, win32con.WS_VISIBLE | win32con.WS_POPUP)
    win32gui.SetLayeredWindowAttributes(hwnd, win32api.RGB(0, 0, 0), 0, win32con.LWA_COLORKEY)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, x, y, width, height, win32con.SWP_SHOWWINDOW)

def draw_dynamic_tapered_arc(surface, cx, cy, radius, intensity, span_angle, side='left'):
    points = []
    half_span = math.radians(span_angle / 2.0)
    steps = 24
    base_angle = math.pi if side == 'left' else 0.0
    
    current_max_thickness = max(4, int(MAX_THICKNESS * intensity))
    
    for i in range(steps + 1):
        t = -half_span + (i / steps) * (half_span * 2)
        angle = base_angle + t
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        points.append((x, y))
        
    for i in range(steps, -1, -1):
        t = -half_span + (i / steps) * (half_span * 2)
        angle = base_angle + t
        t_pct = (t / half_span) * (math.pi / 2)
        current_thickness = current_max_thickness * math.cos(t_pct)
        x = cx + (radius - current_thickness) * math.cos(angle)
        y = cy + (radius - current_thickness) * math.sin(angle)
        points.append((x, y))
        
    arc_surface = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
    alpha_value = int(55 + (200 * intensity))
    pygame.draw.polygon(arc_surface, (*COLOR_ALERT, alpha_value), points)
    surface.blit(arc_surface, (0, 0))

def create_ear_icon():
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.arc([8, 4, 52, 60], start=120, end=380, fill=(245, 30, 30), width=6)
    d.arc([20, 18, 44, 46], start=140, end=360, fill=(245, 30, 30), width=4)
    d.ellipse([28, 28, 36, 36], fill=(245, 30, 30))
    return img

# --- DISPLAY-ADAPTIVE RUNNER ENGINE ---
def run_overlay(monitor_idx, device_idx, min_f, max_f, attack_v, decay_v, gate_v):
    global overlay_running, STEREO_BALANCE_THRESHOLD  
    
    pygame.init()
    
    monitors = win32api.EnumDisplayMonitors()
    monitor_rect = monitors[monitor_idx][2]
    width = monitor_rect[2] - monitor_rect[0]
    height = monitor_rect[3] - monitor_rect[1]
    
    screen = pygame.display.set_mode((width, height), pygame.NOFRAME)
    hwnd = pygame.display.get_wm_info()["window"]
    make_window_click_through(hwnd, monitor_rect)

    p = pyaudio.PyAudio()
    dev_info = p.get_device_info_by_index(device_idx)
    channels = dev_info["maxInputChannels"]
    rate = int(dev_info.get("defaultSampleRate", 48000))
    CHUNK = 1024

    try:
        stream = p.open(
            format=pyaudio.paFloat32, channels=channels, rate=rate,
            input=True, input_device_index=device_idx, frames_per_buffer=CHUNK
        )
    except Exception:
        pygame.quit()
        overlay_running = False
        return

    clock = pygame.time.Clock()
    s_left = s_right = 0.0

    while overlay_running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                overlay_running = False

        try:
            current_monitors = win32api.EnumDisplayMonitors()
            if monitor_idx < len(current_monitors):
                live_rect = current_monitors[monitor_idx][2]
                live_w = live_rect[2] - live_rect[0]
                live_h = live_rect[3] - live_rect[1]
                
                if live_w != width or live_h != height:
                    width, height = live_w, live_h
                    screen = pygame.display.set_mode((width, height), pygame.NOFRAME)
                    hwnd = pygame.display.get_wm_info()["window"]
                
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, live_rect[0], live_rect[1], width, height, win32con.SWP_SHOWWINDOW)
                center_x, center_y = width // 2, height // 2
        except Exception:
            pass

        screen.fill(CLEAN_BACKGROUND_KEY)

        fft_freqs = np.fft.rfftfreq(CHUNK, d=1.0/rate)
        crypto_band_mask = (fft_freqs >= min_f.get()) & (fft_freqs <= max_f.get())

        try:
            data = stream.read(CHUNK, exception_on_overflow=False)
            audio_data = np.frombuffer(data, dtype=np.float32)
            audio_matrix = audio_data.reshape(-1, channels)
        except Exception:
            audio_matrix = np.zeros((CHUNK, channels))

        filtered_strengths = np.zeros(channels)
        if audio_matrix.size > 0:
            for ch in range(channels):
                ch_fft = np.abs(np.fft.rfft(audio_matrix[:, ch]))
                game_frequencies = ch_fft[crypto_band_mask]
                if game_frequencies.size > 0:
                    filtered_strengths[ch] = np.mean(game_frequencies)

        if channels >= 6:
            val_left = max(filtered_strengths[0], filtered_strengths[4])  
            val_right = max(filtered_strengths[1], filtered_strengths[5]) 
        else:
            val_left = filtered_strengths[0]
            val_right = filtered_strengths[1]

        total_lr_power = val_left + val_right
        stereo_delta = abs(val_left - val_right) / (total_lr_power + 1e-5)

        t_left = t_right = 0.0
        max_val = max(val_left, val_right)

        if max_val > gate_v.get() and stereo_delta > STEREO_BALANCE_THRESHOLD:
            if val_left > val_right:
                t_left = min(val_left / 0.18, 1.0)
            else:
                t_right = min(val_right / 0.18, 1.0)

        current_attack = attack_v.get() / 100.0
        current_decay = decay_v.get() / 100.0

        s_left += (t_left - s_left) * (current_attack if t_left > s_left else current_decay)
        s_right += (t_right - s_right) * (current_attack if t_right > s_right else current_decay)

        if s_left > 0.05:
            draw_dynamic_tapered_arc(screen, center_x, center_y, BASE_RADIUS, s_left, SPAN_ANGLE, 'left')

        if s_right > 0.05:
            draw_dynamic_tapered_arc(screen, center_x, center_y, BASE_RADIUS, s_right, SPAN_ANGLE, 'right')

        pygame.display.update()
        clock.tick(144)

    stream.stop_stream()
    stream.close()
    p.terminate()
    pygame.quit()

# --- SAFE NON-BLOCKING TEARDOWN ENGINE ---
def quit_entire_app():
    global overlay_running, tray_icon, root_window
    overlay_running = False
    
    if tray_icon:
        try:
            tray_icon.visible = False
            tray_icon.stop()
        except Exception:
            pass
            
    if root_window:
        try:
            root_window.destroy()
        except Exception:
            pass
    sys.exit(0)

def show_settings_panel():
    global root_window
    if root_window:
        root_window.after(0, lambda: root_window.deiconify())

# --- LAUNCH TRAY SEPARATELY WITHOUT BLOCKING THE MAIN WINDOW ---
def start_tray_icon():
    global tray_icon
    icon_img = create_ear_icon()
    menu = pystray.Menu(
        pystray.MenuItem("Open Configuration Panel", show_settings_panel),
        pystray.MenuItem("Exit Completely", lambda: threading.Thread(target=quit_entire_app).start())
    )
    tray_icon = pystray.Icon("TacticalCompass", icon_img, "Tactical Compass Pro", menu)
    
    # Use detached processing thread specifically for the tray loop
    tray_thread = threading.Thread(target=tray_icon.run, daemon=True)
    tray_thread.start()

# --- MAIN GRAPHICAL CONFIGURATOR WINDOW ---
def launch_gui():
    global overlay_running, pygame_thread, root_window
    
    root = tk.Tk()
    root.title("Tactical Compass Configurator")
    root.geometry("540x820")  
    root.resizable(False, False)
    root_window = root

    title_lbl = tk.Label(root, text="TACTICAL COMPASS OVERLAY", font=("Arial", 14, "bold"), fg="#e12525")
    title_lbl.pack(pady=(12, 2))
    
    dev_lbl = tk.Label(root, text="Developed by: GAMER_PILLA (YouTube)", font=("Arial", 9, "bold"), fg="#555555")
    dev_lbl.pack(pady=(0, 2))

    motto_lbl = tk.Label(root, text='"One deaf to another: happy gaming."', font=("Arial", 10, "italic"), fg="#2b6cb0")
    motto_lbl.pack(pady=(0, 12))

    # --- Hardware Capture Dropdowns ---
    monitors = win32api.EnumDisplayMonitors()
    monitor_options = [f"Monitor {idx+1} ({m[2][2]-m[2][0]}x{m[2][3]-m[2][1]})" for idx, m in enumerate(monitors)]

    tk.Label(root, text="Select Gaming Monitor:", font=("Arial", 9, "bold")).pack(anchor='w', padx=35)
    monitor_box = ttk.Combobox(root, values=monitor_options, width=57, state="readonly")
    monitor_box.pack(pady=4, padx=35)
    monitor_box.current(0)

    p = pyaudio.PyAudio()
    device_options, device_indices = [], []
    for i in range(p.get_device_count()):
        dev_info = p.get_device_info_by_index(i)
        if dev_info.get("isLoopback") or dev_info.get("maxInputChannels") >= 2:
            name = dev_info["name"]
            ch = dev_info["maxInputChannels"]
            if "Input" not in name and ch > 0:
                device_options.append(f"{name} ({ch} Channels)")
                device_indices.append(i)
    p.terminate()

    tk.Label(root, text="Select Headset / Audio Source:", font=("Arial", 9, "bold")).pack(anchor='w', padx=35)
    device_box = ttk.Combobox(root, values=device_options, width=57, state="readonly")
    device_box.pack(pady=4, padx=35)
    if device_options: device_box.current(0)

    # --- Tuning Adjustments ---
    slider_frame = tk.LabelFrame(root, text=" Custom Tuning Adjustments ", font=("Arial", 9, "bold"), padx=15, pady=10)
    slider_frame.pack(pady=10, padx=35, fill="x")

    min_freq_val = tk.IntVar(value=130)
    tk.Label(slider_frame, text="Minimum Frequency (Footstep Thuds / Lows):").pack(anchor='w')
    min_slider = tk.Scale(slider_frame, from_=50, to=500, orient="horizontal", variable=min_freq_val)
    min_slider.pack(fill="x", pady=(0, 6))

    max_freq_val = tk.IntVar(value=2200)
    tk.Label(slider_frame, text="Maximum Frequency (Gunfire / Shield Cracks / Highs):").pack(anchor='w')
    max_slider = tk.Scale(slider_frame, from_=1000, to=4000, orient="horizontal", variable=max_freq_val)
    max_slider.pack(fill="x", pady=(0, 6))

    attack_val = tk.IntVar(value=70)
    tk.Label(slider_frame, text="Responsiveness Speed (Attack Alert Snap):").pack(anchor='w')
    attack_slider = tk.Scale(slider_frame, from_=10, to=100, orient="horizontal", variable=attack_val)
    attack_slider.pack(fill="x", pady=(0, 6))

    decay_val = tk.IntVar(value=5)
    tk.Label(slider_frame, text="Visual Fade Delay (Smooth Arc Bleed Out):").pack(anchor='w')
    decay_slider = tk.Scale(slider_frame, from_=1, to=30, orient="horizontal", variable=decay_val)
    decay_slider.pack(fill="x", pady=(0, 6))

    gate_val = tk.DoubleVar(value=0.055)
    tk.Label(slider_frame, text="Audio Filter Gate Sensitivity (Lower = Catch Distant Sounds):").pack(anchor='w')
    gate_slider = tk.Scale(slider_frame, from_=0.01, to=0.20, resolution=0.005, orient="horizontal", variable=gate_val)
    gate_slider.pack(fill="x")

    # --- Adaptive Presets ---
    profile_frame = tk.Frame(root)
    profile_frame.pack(pady=5, padx=35, fill="x")
    
    tk.Label(profile_frame, text="Quick Competitive Presets:", font=("Arial", 9, "bold")).pack(side="left", padx=(0, 10))
    
    def apply_profile_selection(event):
        selected_name = profile_box.get()
        if selected_name in GAME_PROFILES:
            vals = GAME_PROFILES[selected_name]
            min_freq_val.set(vals[0])
            max_freq_val.set(vals[1])
            attack_val.set(vals[2])
            decay_val.set(vals[3])
            gate_val.set(vals[4])

    profile_box = ttk.Combobox(profile_frame, values=list(GAME_PROFILES.keys()), state="readonly", width=30)
    profile_box.pack(side="left")
    profile_box.current(0)  
    profile_box.bind("<<ComboboxSelected>>", apply_profile_selection)

    # --- Operation Control Action Handles ---
    def toggle_overlay():
        global overlay_running, pygame_thread
        if not overlay_running:
            m_idx = monitor_box.current()
            d_idx = device_indices[device_box.current()] if device_indices else None
            
            if d_idx is not None:
                overlay_running = True
                
                pygame_thread = threading.Thread(
                    target=run_overlay, 
                    args=(m_idx, d_idx, min_freq_val, max_freq_val, attack_val, decay_val, gate_val),
                    daemon=True
                )
                pygame_thread.start()
                root.withdraw()

    action_btn = tk.Button(root, text="LAUNCH TACTICAL COMPASS", font=("Arial", 11, "bold"), 
                           bg="#e12525", fg="white", padx=20, pady=6, command=toggle_overlay)
    action_btn.pack(pady=(15, 4))

    exit_btn = tk.Button(root, text="CLOSE VISUALIZER COMPLETELY", font=("Arial", 9, "bold"), 
                           bg="#4a5568", fg="white", padx=10, pady=4, command=quit_entire_app)
    exit_btn.pack(pady=4)

    # --- REQUIRED SETTING REMINDERS ---
    setup_lbl = tk.Label(root, text="⚙️ REQUIRED: Game must be set to 'BORDERLESS' or 'WINDOWED' mode.", font=("Arial", 9, "bold"), fg="#2b6cb0", wraplength=460)
    setup_lbl.pack(pady=(8, 2))

    warning_lbl = tk.Label(root, text="⚠️ WARNING: Overlay functionality carries anti-cheat detection risks. Use responsibly.", font=("Arial", 9, "bold"), fg="#d32f2f", wraplength=460)
    warning_lbl.pack(pady=(2, 10))

    def on_close_window_event():
        if overlay_running:
            root.withdraw()
        else:
            quit_entire_app()

    root.protocol("WM_DELETE_WINDOW", on_close_window_event)
    
    # Initialize the background system tray loop cleanly
    start_tray_icon()

    root.mainloop()

if __name__ == "__main__":
    launch_gui()