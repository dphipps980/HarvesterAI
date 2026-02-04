import os
import pdfplumber
import pandas as pd
import requests
import time
import random
import re
import concurrent.futures
from math import ceil
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path

# Global variables
config = {
    'api_key': '',
    'pdf_folder': '',
    'questions_file': '',
    'ris_file': '',
    'output_folder': '',
    'test_mode': False,
    'sample_size': 5,
    'max_workers': 15,
    'model': 'deepseek-chat',
    'provider': 'deepseek',
    'temperature': 0.2,
    'top_p': 0.95,
    'system_context': 'You are an expert assistant assisting in extracting data for a systematic review. Your priority should be accuracy and reporting data as is, without unnecessary interpretation.'
}

OUTPUT_EXCEL_LONG = ""
OUTPUT_EXCEL_WIDE = ""
OUTPUT_CSV_TEMP = ""
LOG_FILE = "processing_log.txt"

lock = threading.Lock()
progress_counter = {'completed': 0, 'total': 0, 'failed': []}
processing_thread = None
stop_processing = False

# Windows 11 Color Scheme
COLORS = {
    'bg': '#F3F3F3',           # Light background
    'card': '#FFFFFF',         # Card background
    'accent': '#0078D4',       # Windows 11 blue accent
    'accent_hover': '#106EBE', # Darker blue for hover
    'text': '#1F1F1F',         # Dark text
    'text_secondary': '#616161', # Secondary text
    'border': '#E5E5E5',       # Subtle border
    'success': '#0F7B0F',      # Green for success
    'error': '#C42B1C',        # Red for errors
    'warning': '#CA5010',      # Orange for warnings
}

class ToolTip:
    """Create a tooltip for a given widget"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip = None
        self.widget.bind("<Enter>", self.show_tooltip)
        self.widget.bind("<Leave>", self.hide_tooltip)
    
    def show_tooltip(self, event=None):
        x, y, _, _ = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 25
        
        self.tooltip = tk.Toplevel(self.widget)
        self.tooltip.wm_overrideredirect(True)
        self.tooltip.wm_geometry(f"+{x}+{y}")
        
        label = tk.Label(self.tooltip, text=self.text,
                        background="#FFFFE0", relief="solid",
                        borderwidth=1, font=("Segoe UI", 9),
                        justify=tk.LEFT, padx=8, pady=6)
        label.pack()
    
    def hide_tooltip(self, event=None):
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None

class ModernButton(tk.Canvas):
    """Custom modern button widget with rounded corners"""
    def __init__(self, parent, text, command, **kwargs):
        self.bg_color = kwargs.pop('bg_color', COLORS['accent'])
        self.fg_color = kwargs.pop('fg_color', 'white')
        self.hover_color = kwargs.pop('hover_color', COLORS['accent_hover'])
        self.height = kwargs.pop('height', 36)
        self.width = kwargs.pop('width', 120)
        self.corner_radius = kwargs.pop('corner_radius', 6)
        
        super().__init__(parent, height=self.height, width=self.width, 
                        bg=COLORS['bg'], highlightthickness=0, **kwargs)
        
        self.command = command
        self.text = text
        self.is_hovered = False
        
        # Create rounded rectangle
        self.rect = self._create_rounded_rectangle(
            2, 2, self.width-2, self.height-2, 
            radius=self.corner_radius, fill=self.bg_color
        )
        
        self.text_id = self.create_text(self.width/2, self.height/2, 
                                       text=self.text, fill=self.fg_color,
                                       font=('Segoe UI', 10))
        
        self.bind('<Button-1>', self._on_click)
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)
    
    def _create_rounded_rectangle(self, x1, y1, x2, y2, radius=6, **kwargs):
        """Create a rounded rectangle on canvas"""
        points = [
            x1+radius, y1,
            x1+radius, y1,
            x2-radius, y1,
            x2-radius, y1,
            x2, y1,
            x2, y1+radius,
            x2, y1+radius,
            x2, y2-radius,
            x2, y2-radius,
            x2, y2,
            x2-radius, y2,
            x2-radius, y2,
            x1+radius, y2,
            x1+radius, y2,
            x1, y2,
            x1, y2-radius,
            x1, y2-radius,
            x1, y1+radius,
            x1, y1+radius,
            x1, y1
        ]
        return self.create_polygon(points, smooth=True, **kwargs, outline='')
    
    def _on_click(self, event):
        if self.command:
            self.command()
    
    def _on_enter(self, event):
        self.itemconfig(self.rect, fill=self.hover_color)
        self.is_hovered = True
    
    def _on_leave(self, event):
        self.itemconfig(self.rect, fill=self.bg_color)
        self.is_hovered = False
    
    def set_state(self, state):
        if state == 'disabled':
            self.itemconfig(self.rect, fill='#CCCCCC')
            self.itemconfig(self.text_id, fill='#888888')
            self.unbind('<Button-1>')
            self.unbind('<Enter>')
            self.unbind('<Leave>')
        else:
            self.itemconfig(self.rect, fill=self.bg_color)
            self.itemconfig(self.text_id, fill=self.fg_color)
            self.bind('<Button-1>', self._on_click)
            self.bind('<Enter>', self._on_enter)
            self.bind('<Leave>', self._on_leave)

class DeepSeekExtractorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("HarvesterAI")
        self.root.geometry("900x750")
        self.root.minsize(800, 650)
        
        # Set window background
        self.root.configure(bg=COLORS['bg'])
        
        # Try to add rounded corners (Windows 11)
        try:
            # Windows 11 rounded corners API
            self.root.attributes('-alpha', 0.0)
            self.root.update()
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = 2
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(ctypes.c_int(DWMWCP_ROUND)),
                ctypes.sizeof(ctypes.c_int)
            )
            self.root.attributes('-alpha', 1.0)
        except:
            pass  # Fallback for non-Windows 11 systems
        
        # Configure style
        self.setup_styles()
        
        # Create main container with scrollbar
        main_canvas = tk.Canvas(root, bg=COLORS['bg'], highlightthickness=0)
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=main_canvas.yview)
        scrollable_frame = tk.Frame(main_canvas, bg=COLORS['bg'])
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: main_canvas.configure(scrollregion=main_canvas.bbox("all"))
        )
        
        main_canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        main_canvas.configure(yscrollcommand=scrollbar.set)
        
        # Pack scrollbar and canvas
        scrollbar.pack(side="right", fill="y")
        main_canvas.pack(side="left", fill="both", expand=True)
        
        # Enable mousewheel scrolling
        def on_mousewheel(event):
            main_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        
        main_canvas.bind_all("<MouseWheel>", on_mousewheel)
        
        # Main content container with padding
        main_container = tk.Frame(scrollable_frame, bg=COLORS['bg'])
        main_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        
        # Header
        self.create_header(main_container)
        
        # Content area with tabs
        content_frame = tk.Frame(main_container, bg=COLORS['bg'])
        content_frame.pack(fill=tk.BOTH, expand=True, pady=(15, 0))
        
        # Create notebook (tabs)
        style = ttk.Style()
        style.configure('Modern.TNotebook', background=COLORS['bg'], borderwidth=0)
        style.configure('Modern.TNotebook.Tab', padding=[20, 10], font=('Segoe UI', 10))
        
        self.notebook = ttk.Notebook(content_frame, style='Modern.TNotebook')
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        # Main tab
        main_tab = tk.Frame(self.notebook, bg=COLORS['bg'])
        self.notebook.add(main_tab, text='  Configuration  ')
        
        # Advanced tab
        advanced_tab = tk.Frame(self.notebook, bg=COLORS['bg'])
        self.notebook.add(advanced_tab, text='  Advanced  ')
        
        # Add content to main tab
        self.create_main_tab_content(main_tab)
        
        # Add content to advanced tab
        self.create_advanced_tab_content(advanced_tab)
        
        # Action buttons (outside tabs)
        self.create_action_buttons(content_frame)
        
        # Progress card (outside tabs)
        self.create_progress_card(content_frame)
    
    def create_main_tab_content(self, parent):
        """Create content for main configuration tab"""
        # Configuration card
        self.create_config_card(parent)
        
        # Model selection card
        self.create_model_card(parent)
        
        # Processing options card
        self.create_options_card(parent)
    
    def create_model_card(self, parent):
        """Create model selection card"""
        card = self.create_card(parent, "Model Selection")
        
        model_frame = tk.Frame(card, bg=COLORS['card'])
        model_frame.pack(fill=tk.X, pady=5)
        
        label = ttk.Label(model_frame, text="Provider & Model:", style='Card.TLabel', width=20)
        label.pack(side=tk.LEFT)
        
        self.model_var = tk.StringVar(value='deepseek-chat')
        self.provider_var = tk.StringVar(value='deepseek')
        
        models = [
            ('DeepSeek Chat (Recommended)', 'deepseek-chat', 'deepseek'),
            ('DeepSeek Coder', 'deepseek-coder', 'deepseek'),
            ('OpenAI GPT-4o', 'gpt-4o', 'openai'),
            ('OpenAI GPT-4o Mini', 'gpt-4o-mini', 'openai'),
            ('OpenAI GPT-4 Turbo', 'gpt-4-turbo', 'openai'),
        ]
        
        model_combo_frame = tk.Frame(model_frame, bg='white', relief='solid', bd=1)
        model_combo_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        model_combo = ttk.Combobox(model_combo_frame,
                                   values=[m[0] for m in models],
                                   state='readonly',
                                   font=('Segoe UI', 10))
        model_combo.pack(fill=tk.X, padx=8, pady=6)
        model_combo.set('DeepSeek Chat (Recommended)')
        
        def on_model_select(event):
            display_name = model_combo.get()
            for display, model_id, provider in models:
                if display == display_name:
                    self.model_var.set(model_id)
                    self.provider_var.set(provider)
                    break
        
        model_combo.bind('<<ComboboxSelected>>', on_model_select)
        
        info_label = ttk.Label(model_frame, text="ℹ️", style='Card.TLabel', 
                              foreground=COLORS['accent'])
        info_label.pack(side=tk.LEFT)
        ToolTip(info_label, 
                "DeepSeek Chat: Best value, good quality (~$0.008/PDF)\n"
                "DeepSeek Coder: Better for technical data (~$0.008/PDF)\n\n"
                "OpenAI GPT-4o: Highest quality (~$0.15/PDF, 18x more expensive)\n"
                "OpenAI GPT-4o Mini: Balanced (~$0.02/PDF)\n"
                "OpenAI GPT-4 Turbo: Very good quality (~$0.10/PDF)")
    
    def create_advanced_tab_content(self, parent):
        """Create content for advanced settings tab"""
        # Temperature and Top-P card
        params_card = self.create_card(parent, "Model Parameters")
        
        # Temperature
        temp_frame = tk.Frame(params_card, bg=COLORS['card'])
        temp_frame.pack(fill=tk.X, pady=8)
        
        temp_label_frame = tk.Frame(temp_frame, bg=COLORS['card'])
        temp_label_frame.pack(side=tk.LEFT)
        
        temp_label = ttk.Label(temp_label_frame, text="Temperature:", 
                              style='Card.TLabel', width=20)
        temp_label.pack(side=tk.LEFT)
        
        temp_info = ttk.Label(temp_label_frame, text="ℹ️", style='Card.TLabel',
                             foreground=COLORS['accent'])
        temp_info.pack(side=tk.LEFT, padx=(5, 0))
        ToolTip(temp_info, 
                "Controls randomness in responses:\n" +
                "• 0.0-0.3: Very focused and deterministic (best for extraction)\n" +
                "• 0.4-0.7: Balanced\n" +
                "• 0.8-1.0: More creative\n" +
                "• 1.1-2.0: Very random\n\n" +
                "Recommended: 0.1 for systematic reviews")
        
        self.temperature_var = tk.DoubleVar(value=0.1)
        temp_scale_frame = tk.Frame(temp_frame, bg=COLORS['card'])
        temp_scale_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        temp_scale = tk.Scale(temp_scale_frame, from_=0.0, to=2.0, resolution=0.1,
                             orient=tk.HORIZONTAL, variable=self.temperature_var,
                             bg=COLORS['card'], fg=COLORS['text'],
                             highlightthickness=0, troughcolor=COLORS['border'],
                             activebackground=COLORS['accent'])
        temp_scale.pack(fill=tk.X, side=tk.LEFT, expand=True)
        
        temp_value = ttk.Label(temp_frame, textvariable=self.temperature_var,
                              style='Card.TLabel', width=5)
        temp_value.pack(side=tk.LEFT)
        
        # Top P
        topp_frame = tk.Frame(params_card, bg=COLORS['card'])
        topp_frame.pack(fill=tk.X, pady=8)
        
        topp_label_frame = tk.Frame(topp_frame, bg=COLORS['card'])
        topp_label_frame.pack(side=tk.LEFT)
        
        topp_label = ttk.Label(topp_label_frame, text="Top P:", 
                              style='Card.TLabel', width=20)
        topp_label.pack(side=tk.LEFT)
        
        topp_info = ttk.Label(topp_label_frame, text="ℹ️", style='Card.TLabel',
                             foreground=COLORS['accent'])
        topp_info.pack(side=tk.LEFT, padx=(5, 0))
        ToolTip(topp_info,
                "Controls diversity of word choices:\n" +
                "• 0.1: Only consider top 10% most likely words\n" +
                "• 0.5: Consider top 50% most likely words\n" +
                "• 0.9-1.0: Consider almost all words (more diverse)\n\n" +
                "Recommended: 0.95 for balanced extraction")
        
        self.top_p_var = tk.DoubleVar(value=0.95)
        topp_scale_frame = tk.Frame(topp_frame, bg=COLORS['card'])
        topp_scale_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        topp_scale = tk.Scale(topp_scale_frame, from_=0.1, to=1.0, resolution=0.05,
                             orient=tk.HORIZONTAL, variable=self.top_p_var,
                             bg=COLORS['card'], fg=COLORS['text'],
                             highlightthickness=0, troughcolor=COLORS['border'],
                             activebackground=COLORS['accent'])
        topp_scale.pack(fill=tk.X, side=tk.LEFT, expand=True)
        
        topp_value = ttk.Label(topp_frame, textvariable=self.top_p_var,
                              style='Card.TLabel', width=5)
        topp_value.pack(side=tk.LEFT)
        
        # System context card
        context_card = self.create_card(parent, "System Context")
        
        context_label = ttk.Label(context_card, 
                                 text="This message sets the AI's role and behavior. Edit to customize how the AI approaches extraction.",
                                 style='Card.TLabel',
                                 foreground=COLORS['text_secondary'],
                                 wraplength=700)
        context_label.pack(anchor='w', pady=(0, 10))
        
        text_frame = tk.Frame(context_card, bg='white', relief='solid', bd=1)
        text_frame.pack(fill=tk.BOTH, expand=True)
        
        self.system_context_text = tk.Text(text_frame, 
                                           height=6,
                                           wrap=tk.WORD,
                                           font=('Segoe UI', 10),
                                           bg='white',
                                           fg=COLORS['text'],
                                           relief='flat',
                                           bd=0)
        self.system_context_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        self.system_context_text.insert('1.0', config['system_context'])
        ToolTip(self.system_context_text, "Edit the system prompt to customize how the AI approaches data extraction from PDFs. For example, you could tell the AI the purpose of your review to ensure it has context.")
        
        # Reset button
        reset_frame = tk.Frame(context_card, bg=COLORS['card'])
        reset_frame.pack(fill=tk.X, pady=(10, 0))
        
        reset_btn = ModernButton(reset_frame, "Reset to Default", 
                                self.reset_system_context,
                                width=140, height=32,
                                bg_color=COLORS['border'],
                                fg_color=COLORS['text'],
                                hover_color='#D0D0D0')
        reset_btn.pack(side=tk.LEFT)
    
    def reset_system_context(self):
        """Reset system context to default"""
        default_context = 'You are an expert assistant assisting in extracting data for a systematic review. Your priority should be accuracy and reporting data as is, without unnecessary interpretation.'
        self.system_context_text.delete('1.0', tk.END)
        self.system_context_text.insert('1.0', default_context)
        
        # Status bar
        self.create_status_bar(main_container)
    
    def setup_styles(self):
        """Setup ttk styles for modern look"""
        style = ttk.Style()
        
        # Configure modern entry style
        style.configure('Modern.TEntry',
                       fieldbackground='white',
                       borderwidth=1,
                       relief='solid')
        
        # Configure modern label style
        style.configure('Card.TLabel',
                       background=COLORS['card'],
                       foreground=COLORS['text'],
                       font=('Segoe UI', 10))
        
        style.configure('CardTitle.TLabel',
                       background=COLORS['card'],
                       foreground=COLORS['text'],
                       font=('Segoe UI', 11, 'bold'))
        
        style.configure('Header.TLabel',
                       background=COLORS['bg'],
                       foreground=COLORS['text'],
                       font=('Segoe UI', 24, 'bold'))
        
        style.configure('Subtitle.TLabel',
                       background=COLORS['bg'],
                       foreground=COLORS['text_secondary'],
                       font=('Segoe UI', 10))
        
        # Modern checkbutton
        style.configure('Modern.TCheckbutton',
                       background=COLORS['card'],
                       foreground=COLORS['text'],
                       font=('Segoe UI', 10))
        
        # Modern progressbar
        style.configure('Modern.Horizontal.TProgressbar',
                       troughcolor=COLORS['border'],
                       background=COLORS['accent'],
                       borderwidth=0,
                       thickness=8)
    
    def create_card(self, parent, title=None):
        """Create a modern card container with rounded corners effect"""
        # Outer frame for shadow/border effect
        outer_frame = tk.Frame(parent, bg=COLORS['border'], relief='flat', bd=0)
        outer_frame.pack(fill=tk.X, pady=(0, 12), padx=2)
        
        # Inner card frame
        card_frame = tk.Frame(outer_frame, bg=COLORS['card'], relief='flat', bd=0)
        card_frame.pack(fill=tk.X, padx=1, pady=1)
        
        content = tk.Frame(card_frame, bg=COLORS['card'])
        content.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)
        
        if title:
            title_label = ttk.Label(content, text=title, style='CardTitle.TLabel')
            title_label.pack(anchor='w', pady=(0, 12))
        
        return content
    
    def create_header(self, parent):
        """Create modern header"""
        header_frame = tk.Frame(parent, bg=COLORS['bg'])
        header_frame.pack(fill=tk.X, pady=(0, 20))
        
        title = ttk.Label(header_frame, text="HarvesterAI", style='Header.TLabel')
        title.pack(anchor='w')
        
        # Subtitle with clickable link
        subtitle_frame = tk.Frame(header_frame, bg=COLORS['bg'])
        subtitle_frame.pack(anchor='w', pady=(5, 0))
        
        subtitle_text = "A Systematic review tool for structured extraction from PDFs\nDaniel Phipps • 2026 • v0.0.1 • "
        subtitle_label = ttk.Label(subtitle_frame, text=subtitle_text, style='Subtitle.TLabel')
        subtitle_label.pack(side=tk.LEFT)
        
        link_label = ttk.Label(subtitle_frame, text="danielphipps.info", 
                              style='Subtitle.TLabel',
                              foreground=COLORS['accent'],
                              cursor="hand2")
        link_label.pack(side=tk.LEFT)
        link_label.bind("<Button-1>", lambda e: self.open_url("https://www.danielphipps.info/"))
        link_label.bind("<Enter>", lambda e: link_label.configure(font=('Segoe UI', 10, 'underline')))
        link_label.bind("<Leave>", lambda e: link_label.configure(font=('Segoe UI', 10)))
    
    def open_url(self, url):
        """Open URL in browser"""
        import webbrowser
        webbrowser.open(url)
    
    def create_config_card(self, parent):
        """Create configuration card"""
        card = self.create_card(parent, "Configuration")
        
        # API Key
        self.create_input_row(card, "API Key", 'api_key', is_password=True)
        
        # PDF Folder
        self.create_file_row(card, "PDF Folder", 'pdf_folder', folder=True)
        
        # Questions File
        self.create_file_row(card, "Questions File", 'questions_file', 
                           filetypes=[("Excel files", "*.xlsx")])
        
        # RIS File
        self.create_file_row(card, "RIS File (Optional)", 'ris_file',
                           filetypes=[("RIS files", "*.ris")])
        
        # Output Folder
        self.create_file_row(card, "Output Folder", 'output_folder', folder=True)
    
    def create_input_row(self, parent, label_text, var_name, is_password=False):
        """Create a modern input row"""
        row = tk.Frame(parent, bg=COLORS['card'])
        row.pack(fill=tk.X, pady=8)
        
        label = ttk.Label(row, text=label_text, style='Card.TLabel', width=20)
        label.pack(side=tk.LEFT)
        
        # Container for rounded effect
        entry_container = tk.Frame(row, bg=COLORS['border'], relief='flat')
        entry_container.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        entry_frame = tk.Frame(entry_container, bg='white', relief='flat', bd=0)
        entry_frame.pack(fill=tk.BOTH, padx=1, pady=1)
        
        var = tk.StringVar()
        setattr(self, f'{var_name}_var', var)
        
        entry = tk.Entry(entry_frame, textvariable=var, 
                        font=('Segoe UI', 10),
                        bg='white', fg=COLORS['text'],
                        relief='flat', bd=0,
                        show='●' if is_password else '')
        entry.pack(fill=tk.BOTH, padx=12, pady=8)
        
        if is_password:
            setattr(self, f'{var_name}_entry', entry)
            
            btn = ModernButton(row, "👁", self.toggle_api_key,
                             width=40, height=32,
                             bg_color=COLORS['border'],
                             fg_color=COLORS['text'],
                             hover_color='#D0D0D0',
                             corner_radius=4)
            btn.pack(side=tk.LEFT)
    
    def create_file_row(self, parent, label_text, var_name, folder=False, filetypes=None):
        """Create a modern file selection row"""
        row = tk.Frame(parent, bg=COLORS['card'])
        row.pack(fill=tk.X, pady=8)
        
        label = ttk.Label(row, text=label_text, style='Card.TLabel', width=20)
        label.pack(side=tk.LEFT)
        
        # Container for rounded effect
        entry_container = tk.Frame(row, bg=COLORS['border'], relief='flat')
        entry_container.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        entry_frame = tk.Frame(entry_container, bg='white', relief='flat', bd=0)
        entry_frame.pack(fill=tk.BOTH, padx=1, pady=1)
        
        var = tk.StringVar()
        setattr(self, f'{var_name}_var', var)
        
        entry = tk.Entry(entry_frame, textvariable=var,
                        font=('Segoe UI', 10),
                        bg='white', fg=COLORS['text'],
                        relief='flat', bd=0, state='readonly')
        entry.pack(fill=tk.BOTH, padx=12, pady=8)
        
        if folder:
            cmd = lambda: self.browse_folder(var)
        else:
            cmd = lambda: self.browse_file(var, filetypes)
        
        btn = ModernButton(row, "Browse", cmd,
                         width=90, height=32,
                         bg_color=COLORS['accent'],
                         hover_color=COLORS['accent_hover'],
                         corner_radius=4)
        btn.pack(side=tk.LEFT)
        
        setattr(self, f'{var_name}_button', btn)
    
    def create_options_card(self, parent):
        """Create processing options card"""
        card = self.create_card(parent, "Processing Options")
        
        # Test mode
        test_frame = tk.Frame(card, bg=COLORS['card'])
        test_frame.pack(fill=tk.X, pady=5)
        
        self.test_mode_var = tk.BooleanVar(value=False)
        test_check = ttk.Checkbutton(test_frame, 
                                    text="Test Mode (process only a sample)",
                                    variable=self.test_mode_var,
                                    command=self.toggle_test_mode,
                                    style='Modern.TCheckbutton')
        test_check.pack(anchor='w')
        
        # Sample size (hidden by default)
        self.sample_frame = tk.Frame(card, bg=COLORS['card'])
        
        sample_label = ttk.Label(self.sample_frame, text="Sample Size:", 
                                style='Card.TLabel')
        sample_label.pack(side=tk.LEFT, padx=(30, 10))
        
        self.sample_size_var = tk.IntVar(value=5)
        sample_spin = ttk.Spinbox(self.sample_frame, from_=1, to=50,
                                 textvariable=self.sample_size_var,
                                 width=10, font=('Segoe UI', 10))
        sample_spin.pack(side=tk.LEFT)
        
        # Threads
        thread_frame = tk.Frame(card, bg=COLORS['card'])
        thread_frame.pack(fill=tk.X, pady=(15, 5))
        
        thread_label = ttk.Label(thread_frame, text="Parallel Threads:",
                               style='Card.TLabel')
        thread_label.pack(side=tk.LEFT, padx=(0, 10))
        
        self.max_workers_var = tk.IntVar(value=20)
        thread_spin = ttk.Spinbox(thread_frame, from_=1, to=100,
                                 textvariable=self.max_workers_var,
                                 width=10, font=('Segoe UI', 10))
        thread_spin.pack(side=tk.LEFT)
        
        thread_hint = ttk.Label(thread_frame, text="(Recommended: 10-50)",
                              style='Card.TLabel',
                              foreground=COLORS['text_secondary'])
        thread_hint.pack(side=tk.LEFT, padx=(10, 0))
    
    def create_action_buttons(self, parent):
        """Create action buttons"""
        button_frame = tk.Frame(parent, bg=COLORS['bg'])
        button_frame.pack(fill=tk.X, pady=15)
        
        # Start button
        self.start_button = ModernButton(button_frame, "▶ Start Processing",
                                        self.start_processing,
                                        width=180, height=40,
                                        bg_color=COLORS['success'],
                                        hover_color='#0D690D')
        self.start_button.pack(side=tk.LEFT, padx=(0, 10))
        
        # Stop button
        self.stop_button = ModernButton(button_frame, "⬛ Stop",
                                       self.stop_processing_action,
                                       width=120, height=40,
                                       bg_color=COLORS['error'],
                                       hover_color='#A02316')
        self.stop_button.pack(side=tk.LEFT, padx=(0, 10))
        self.stop_button.set_state('disabled')
        
        # Open folder button
        self.folder_button = ModernButton(button_frame, "📁 Open Output",
                                         self.open_output_folder,
                                         width=140, height=40,
                                         bg_color=COLORS['accent'],
                                         hover_color=COLORS['accent_hover'])
        self.folder_button.pack(side=tk.LEFT)
    
    def create_progress_card(self, parent):
        """Create progress card"""
        card = self.create_card(parent, "Progress")
        card.master.pack_configure(fill=tk.BOTH, expand=True)
        
        # Progress bar
        self.progress_var = tk.DoubleVar()
        progress_frame = tk.Frame(card, bg=COLORS['card'])
        progress_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.progress_bar = ttk.Progressbar(progress_frame,
                                           variable=self.progress_var,
                                           maximum=100,
                                           style='Modern.Horizontal.TProgressbar',
                                           length=400)
        self.progress_bar.pack(fill=tk.X)
        
        # Status text
        text_frame = tk.Frame(card, bg='white', relief='solid', bd=1)
        text_frame.pack(fill=tk.BOTH, expand=True)
        
        self.status_text = scrolledtext.ScrolledText(text_frame,
                                                     wrap=tk.WORD,
                                                     font=('Consolas', 9),
                                                     bg='white',
                                                     fg=COLORS['text'],
                                                     relief='flat',
                                                     bd=0,
                                                     state='disabled')
        self.status_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        
        # Configure text tags for colored output
        self.status_text.tag_config('info', foreground=COLORS['text'])
        self.status_text.tag_config('success', foreground=COLORS['success'])
        self.status_text.tag_config('error', foreground=COLORS['error'])
        self.status_text.tag_config('warning', foreground=COLORS['warning'])
    
    def create_status_bar(self, parent):
        """Create bottom status bar"""
        status_frame = tk.Frame(parent, bg=COLORS['card'], height=40)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM, pady=(15, 0))
        
        # Subtle top border
        border = tk.Frame(status_frame, bg=COLORS['border'], height=1)
        border.pack(fill=tk.X, side=tk.TOP)
        
        content = tk.Frame(status_frame, bg=COLORS['card'])
        content.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        self.status_label = ttk.Label(content, text="Ready",
                                     style='Card.TLabel',
                                     foreground=COLORS['text_secondary'])
        self.status_label.pack(side=tk.LEFT)
        
        # GitHub link on right
        github_link = ttk.Label(content, 
                               text="github.com/dphipps980/HarvesterAI",
                               style='Card.TLabel',
                               foreground=COLORS['accent'],
                               cursor="hand2")
        github_link.pack(side=tk.RIGHT)
        github_link.bind("<Button-1>", lambda e: self.open_url("https://github.com/dphipps980/HarvesterAI/"))
        github_link.bind("<Enter>", lambda e: github_link.configure(font=('Segoe UI', 10, 'underline')))
        github_link.bind("<Leave>", lambda e: github_link.configure(font=('Segoe UI', 10)))
    
    def toggle_api_key(self):
        """Toggle API key visibility"""
        if hasattr(self, 'api_key_entry'):
            if self.api_key_entry.cget('show') == '●':
                self.api_key_entry.config(show='')
            else:
                self.api_key_entry.config(show='●')
    
    def toggle_test_mode(self):
        """Show/hide sample size"""
        if self.test_mode_var.get():
            self.sample_frame.pack(fill=tk.X, pady=5, after=self.sample_frame.master.winfo_children()[0])
        else:
            self.sample_frame.pack_forget()
    
    def browse_folder(self, var):
        folder = filedialog.askdirectory()
        if folder:
            var.set(folder)
    
    def browse_file(self, var, filetypes=None):
        file = filedialog.askopenfilename(filetypes=filetypes or [("All files", "*.*")])
        if file:
            var.set(file)
    
    def open_output_folder(self):
        folder = self.output_folder_var.get()
        if folder and os.path.exists(folder):
            os.startfile(folder)
        else:
            messagebox.showwarning("Warning", "Output folder not set or doesn't exist!")
    
    def log_to_gui(self, message, important=False):
        """Add message to GUI log with color coding"""
        self.status_text.config(state='normal')
        timestamp = time.strftime("%H:%M:%S")
        
        # Determine tag based on message content
        tag = 'info'
        if 'ERROR' in message or 'FAILED' in message:
            tag = 'error'
        elif 'SUCCESS' in message or 'Complete' in message:
            tag = 'success'
        elif 'WARNING' in message:
            tag = 'warning'
        
        self.status_text.insert(tk.END, f"[{timestamp}] {message}\n", tag)
        self.status_text.see(tk.END)
        self.status_text.config(state='disabled')
        
        # Write to file
        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"[{timestamp}] {message}\n")
    
    def update_status(self, message):
        """Update status label"""
        if hasattr(self, 'status_label'):
            self.status_label.config(text=message)
    
    def update_progress(self, current, total):
        """Update progress bar"""
        if total > 0:
            percentage = (current / total) * 100
            self.progress_var.set(percentage)
            self.update_status(f"Processing: {current}/{total} PDFs ({percentage:.1f}%)")
    
    def validate_config(self):
        """Validate configuration"""
        if not self.api_key_var.get():
            messagebox.showerror("Error", "API Key is required!")
            return False
        
        if not self.pdf_folder_var.get() or not os.path.exists(self.pdf_folder_var.get()):
            messagebox.showerror("Error", "PDF Folder is required and must exist!")
            return False
        
        if not self.questions_file_var.get() or not os.path.exists(self.questions_file_var.get()):
            messagebox.showerror("Error", "Questions File is required and must exist!")
            return False
        
        if not self.output_folder_var.get():
            messagebox.showerror("Error", "Output Folder is required!")
            return False
        
        if not os.path.exists(self.output_folder_var.get()):
            try:
                os.makedirs(self.output_folder_var.get(), exist_ok=True)
            except Exception as e:
                messagebox.showerror("Error", f"Could not create output folder: {e}")
                return False
        
        return True
    
    def start_processing(self):
        """Start processing"""
        if not self.validate_config():
            return
        
        config['api_key'] = self.api_key_var.get()
        config['pdf_folder'] = self.pdf_folder_var.get()
        config['questions_file'] = self.questions_file_var.get()
        config['ris_file'] = self.ris_file_var.get()
        config['output_folder'] = self.output_folder_var.get()
        config['test_mode'] = self.test_mode_var.get()
        config['sample_size'] = self.sample_size_var.get()
        config['max_workers'] = self.max_workers_var.get()
        
        # Advanced settings
        config['model'] = self.model_var.get()
        config['provider'] = self.provider_var.get()
        config['temperature'] = self.temperature_var.get()
        config['top_p'] = self.top_p_var.get()
        config['system_context'] = self.system_context_text.get('1.0', tk.END).strip()
        
        mode_text = f"TEST MODE ({config['sample_size']} PDFs)" if config['test_mode'] else "FULL MODE (all PDFs)"
        provider_display = "DeepSeek" if config['provider'] == 'deepseek' else "OpenAI"
        if not messagebox.askyesno("Confirm",
                                   f"Start processing in {mode_text} with {config['max_workers']} threads?\n\n"
                                   f"Provider: {provider_display}\n"
                                   f"Model: {config['model']}\n"
                                   f"Temperature: {config['temperature']}\n"
                                   f"This may take a while."):
            return
        
        self.start_button.set_state('disabled')
        self.stop_button.set_state('normal')
        
        self.status_text.config(state='normal')
        self.status_text.delete(1.0, tk.END)
        self.status_text.config(state='disabled')
        
        self.progress_var.set(0)
        
        global stop_processing
        stop_processing = False
        processing_thread = threading.Thread(target=self.run_processing, daemon=True)
        processing_thread.start()
    
    def stop_processing_action(self):
        """Stop processing"""
        global stop_processing
        stop_processing = True
        self.log_to_gui("STOP REQUESTED - Finishing current PDFs...", True)
        self.stop_button.set_state('disabled')
    
    def run_processing(self):
        """Run processing in separate thread"""
        try:
            from deepseek_processing import process_pdfs
            
            process_pdfs(config, self.log_to_gui, self.update_progress, self.update_status)
            
            self.start_button.set_state('normal')
            self.stop_button.set_state('disabled')
            
            self.log_to_gui("=== PROCESSING COMPLETE ===", True)
            self.update_status("Complete!")
            
            messagebox.showinfo("Complete", "Processing finished! Check the output folder for results.")
            
        except Exception as e:
            self.log_to_gui(f"ERROR: {e}", True)
            self.log_to_gui(traceback.format_exc(), True)
            self.start_button.set_state('normal')
            self.stop_button.set_state('disabled')
            messagebox.showerror("Error", f"An error occurred:\n{e}")

def main():
    root = tk.Tk()
    
    # Try to set Windows 11 theme if available
    try:
        root.tk.call("source", "azure.tcl")
        root.tk.call("set_theme", "light")
    except:
        pass
    
    app = DeepSeekExtractorGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
