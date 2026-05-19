# ============================================
#   DROWSINESS DETECTION SYSTEM 
# ============================================

import cv2
import dlib
import numpy as np
import os
import time
import signal
import sys
import threading
import subprocess
from imutils import face_utils

# Kill any leftover audio processes (prevents multiple espeak/aplay overlap)
os.system("pkill -f espeak >/dev/null 2>&1 || true")
os.system("pkill -f aplay >/dev/null 2>&1 || true")

# ============================================
# GPIO SETUP 
# ============================================
import RPi.GPIO as GPIO
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

BUZZER = 23
GPIO.setup(BUZZER, GPIO.OUT)

# Active LOW buzzer → HIGH keeps it OFF initially
GPIO.output(BUZZER, GPIO.HIGH)

def buzzer_on():
    GPIO.output(BUZZER, GPIO.LOW)

def buzzer_off():
    GPIO.output(BUZZER, GPIO.HIGH)

# ============================================
# WS2813 LED STRIP SETUP -Using rpi_ws281x library
# ============================================
from rpi_ws281x import PixelStrip, Color

LED_COUNT = 10           # Number of LEDs on RGB stick
LED_PIN = 21             # DIN connected to GPIO21
LED_FREQ = 800000
LED_DMA = 10
LED_BRIGHTNESS = 150
LED_INVERT = False
LED_CHANNEL = 0

def init_strip():
    """Initialize the LED strip safely."""
    s = PixelStrip(
        LED_COUNT, LED_PIN, LED_FREQ, LED_DMA,
        LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL
    )
    try:
        s.begin()   # Start the LED driver
    except Exception as e:
        print("Warning: LED strip failed to initialize:", e)
    return s

strip = init_strip()

def set_led_all(r, g, b):
    """Set all LEDs to the given RGB color."""
    try:
        # WS2813 uses GRB order instead of RGB
        for i in range(strip.numPixels()):
            strip.setPixelColor(i, Color(g, r, b))
        strip.show()
    except Exception:
        pass  # Ignore strip errors if driver resets

# Shortcuts for LED status colors
def led_blue():  set_led_all(0,0,255)
def led_green(): set_led_all(255,0,0)
def led_red():   set_led_all(0,255,0)
def led_off():   set_led_all(0,0,0)

def reset_led_strip():
    """Full reset for stuck LED strip conditions."""
    global strip

    # Attempt multiple clears
    for _ in range(3):
        try:
            for i in range(strip.numPixels()):
                strip.setPixelColor(i, Color(0,0,0))
            strip.show()
        except:
            pass
        time.sleep(0.05)

    # Attempt library driver cleanup if available
    try:
        if hasattr(strip, "_cleanup"):
            strip._cleanup()
    except:
        pass

    # Reinitialize after cleanup
    strip = init_strip()
    led_off()

# ============================================
# ALERT SYSTEM (Buzzer + Voice + LED)
# ============================================
alarm_on = False
alert_thread = None
alert_lock = threading.Lock()

def alert_loop():
    """Loop that plays buzzer + voice alert repeatedly until alarm stops."""
    global alarm_on

    while alarm_on:
        try:
            led_red()      # Show danger
            buzzer_on()    # Turn ON buzzer

            # Generate voice → espeak outputs raw PCM → piped into aplay to play on speaker
            p_es = subprocess.Popen(
                ["espeak", "-s", "145", "-p", "25", "-a", "200",
                 "--stdout", "Wake up driver"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            p_aplay = subprocess.Popen(
                ["aplay", "-q", "-D", "hw:2,0"],  # Play via USB sound card
                stdin=p_es.stdout,
                stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL
            )
            p_es.stdout.close()

        except Exception as e:
            print("[alert_loop] error:", e)

        # Control buzzer pattern
        time.sleep(0.7)
        buzzer_off()
        time.sleep(0.4)

    # Clean leftover audio when alarm stops
    os.system("pkill -f espeak >/dev/null 2>&1 || true")
    os.system("pkill -f aplay >/dev/null 2>&1 || true")


def trigger_alert():
    """Start alert loop thread only once."""
    global alert_thread
    with alert_lock:
        if alert_thread and alert_thread.is_alive():
            return
        alert_thread = threading.Thread(target=alert_loop, daemon=True)
        alert_thread.start()

# ============================================
# DLIB FUNCTIONS FOR EAR (Eye Aspect Ratio)
# ============================================
def eye_aspect_ratio(eye):
    """Compute EAR for one eye using landmark distances."""
    A = np.linalg.norm(eye[1] - eye[5])  # Vertical distances
    B = np.linalg.norm(eye[2] - eye[4])
    C = np.linalg.norm(eye[0] - eye[3])  # Horizontal distance

    if C == 0:
        return 0
    return (A + B) / (2.0 * C)   # Standard EAR formula

def get_ear(shape):
    """Extract left/right eye landmarks + compute average EAR."""
    (lStart, lEnd) = face_utils.FACIAL_LANDMARKS_IDXS["left_eye"]
    (rStart, rEnd) = face_utils.FACIAL_LANDMARKS_IDXS["right_eye"]

    leftEye = shape[lStart:lEnd]
    rightEye = shape[rStart:rEnd]

    return (eye_aspect_ratio(leftEye) + eye_aspect_ratio(rightEye)) / 2.0

# ============================================
# Load Dlib Model
# ============================================
MODEL = "shape_predictor_68_face_landmarks.dat"
if not os.path.exists(MODEL):
    print("❌ ERROR: Required dlib model missing:", MODEL)
    sys.exit(1)

detector = dlib.get_frontal_face_detector()     # Face detector
predictor = dlib.shape_predictor(MODEL)         # Landmark predictor

# ============================================
# Initialize camera
# ============================================
cap = cv2.VideoCapture(0)
time.sleep(1)

EAR_THRESH = 0.20      # EAR threshold for drowsiness
FRAME_LIMIT = 10        # Minimum consecutive frames needed
counter = 0

# ============================================
# Cleanup Handler (Runs on Ctrl+C or exit)
# ============================================
def cleanup(sig=None, frame=None):
    """Safely shutdown camera, LEDs, buzzer and kill audio."""
    global alarm_on, alert_thread
    print("\n🛑 Cleanup starting...")

    alarm_on = False
    time.sleep(0.2)

    # Wait for alert thread to finish
    with alert_lock:
        if alert_thread and alert_thread.is_alive():
            alert_thread.join(timeout=2)

    # Turn off hardware
    buzzer_off()
    reset_led_strip()

    # Release camera + GUI windows
    cap.release()
    cv2.destroyAllWindows()

    # Kill audio
    os.system("pkill -f espeak >/dev/null 2>&1 || true")
    os.system("pkill -f aplay >/dev/null 2>&1 || true")

    GPIO.cleanup()
    print("✔ Cleanup complete. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# ============================================
# MAIN LOOP
# ============================================
print("✅ Drowsiness monitor running...")

try:
    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.05)
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector(gray)

        # ------------------------------------------
        # CASE 1: NO FACE DETECTED
        # ------------------------------------------
        if len(faces) == 0:
            led_blue()          # Driver missing
            counter = 0
            alarm_on = False
            buzzer_off()
        else:
            # Select the largest face assuming it's the driver
            faces = sorted(faces, key=lambda r: r.width()*r.height(), reverse=True)
            face = faces[0]

            # Landmark detection
            shape = predictor(gray, face)
            shape = face_utils.shape_to_np(shape)

            ear = get_ear(shape)

            # Draw convex hull around both eyes for debugging
            (lStart, lEnd) = face_utils.FACIAL_LANDMARKS_IDXS["left_eye"]
            (rStart, rEnd) = face_utils.FACIAL_LANDMARKS_IDXS["right_eye"]
            cv2.drawContours(frame, [cv2.convexHull(shape[lStart:lEnd])], -1, (0,255,0), 1)
            cv2.drawContours(frame, [cv2.convexHull(shape[rStart:rEnd])], -1, (0,255,0), 1)

            # ------------------------------------------
            # CASE 2: EYES CLOSED -> DROWSINESS RISK
            # ------------------------------------------
            if ear < EAR_THRESH:
                counter += 1
                if counter >= FRAME_LIMIT and not alarm_on:
                    print("⚠ DROWSINESS DETECTED!")
                    alarm_on = True
                    trigger_alert()
            else:
                # Eyes open → reset alert + show green LED
                counter = 0
                if alarm_on:
                    alarm_on = False
                    os.system("pkill -f espeak >/dev/null 2>&1 || true")
                    os.system("pkill -f aplay >/dev/null 2>&1 || true")
                    buzzer_off()
                led_green()

        # Display EAR value on screen
        try:
            cv2.putText(frame, f"EAR: {ear:.2f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
        except:
            pass

        cv2.imshow("Drowsiness Monitor", frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            cleanup()

except Exception as e:
    print("Exception in main loop:", e)
    cleanup()
