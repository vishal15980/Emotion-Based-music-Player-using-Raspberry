"""Emotion-based music player for Raspberry Pi 4.

The camera preview is processed locally. A playlist changes only after a
supported emotion has appeared in at least two of the last three analyses.
"""

from collections import Counter, defaultdict, deque
from pathlib import Path
import random
import time

import cv2
import pygame
from deepface import DeepFace


# Keep playlists beside this script so the project works after cloning it.
PROJECT_DIR = Path(__file__).resolve().parent
PLAYLIST_DIR = PROJECT_DIR / "Playlist"
PLAYLISTS = {
    "happy": PLAYLIST_DIR / "Happy",
    "sad": PLAYLIST_DIR / "Sad",
    "fear": PLAYLIST_DIR / "Fear",
}

# DeepFace can detect other labels too. Only these labels select a playlist.
EMOTION_TO_PLAYLIST = {
    "happy": "happy",
    "sad": "sad",
    "fear": "fear",
}

CAMERA_INDEX = 0
ANALYSIS_INTERVAL_SECONDS = 3.0
MAX_FRAME_WIDTH = 640
SUPPORTED_AUDIO = {".mp3", ".wav", ".ogg"}


class MusicPlayer:
    """Choose and play shuffled tracks from the current emotion folder."""

    def __init__(self):
        self.emotion = None
        self.current_song = None
        self.queue = deque()
        self.played = defaultdict(set)
        self.paused = False
        self.status = "Waiting for a supported emotion"

    def _tracks_for(self, emotion):
        folder = PLAYLISTS[emotion]
        if not folder.is_dir():
            return []

        return sorted(
            path for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO
        )

    def _fill_queue(self, tracks):
        unplayed = [track for track in tracks if track not in self.played[self.emotion]]
        if not unplayed:
            # Start a new shuffle cycle after every song in this folder has played.
            self.played[self.emotion].clear()
            unplayed = list(tracks)

        random.shuffle(unplayed)
        self.queue.extend(unplayed)

    def select_emotion(self, emotion):
        if emotion == self.emotion:
            return

        pygame.mixer.music.stop()
        self.emotion = emotion
        self.current_song = None
        self.paused = False
        self.queue.clear()
        print(f"Selected playlist: {emotion}")
        self.play_next()

    def play_next(self):
        if self.emotion is None:
            return

        tracks = self._tracks_for(self.emotion)
        if not tracks:
            folder = PLAYLISTS[self.emotion]
            self.current_song = None
            self.status = f"No playable songs in {folder.relative_to(PROJECT_DIR)}"
            print(self.status)
            return

        # Try each track at most once in this call in case a file is unsupported.
        attempts_left = len(tracks)
        while attempts_left > 0:
            if not self.queue:
                self._fill_queue(tracks)
            if not self.queue:
                break

            track = self.queue.popleft()
            attempts_left -= 1
            try:
                pygame.mixer.music.load(str(track))
                pygame.mixer.music.play()
            except pygame.error as error:
                self.played[self.emotion].add(track)
                print(f"Could not play {track.name}: {error}")
                continue

            self.played[self.emotion].add(track)
            self.current_song = track
            self.paused = False
            self.status = f"Playing: {track.name}"
            print(self.status)
            return

        self.current_song = None
        self.status = f"No playable songs in {PLAYLISTS[self.emotion].name}"
        print(self.status)

    def update(self):
        if (
            self.current_song is not None
            and not self.paused
            and not pygame.mixer.music.get_busy()
        ):
            self.current_song = None
            self.play_next()

    def skip(self):
        if self.emotion is None:
            return
        pygame.mixer.music.stop()
        self.current_song = None
        self.paused = False
        self.play_next()

    def toggle_pause(self):
        if self.current_song is None:
            return

        if self.paused:
            pygame.mixer.music.unpause()
            self.paused = False
            self.status = f"Playing: {self.current_song.name}"
        else:
            pygame.mixer.music.pause()
            self.paused = True
            self.status = f"Paused: {self.current_song.name}"

    def close(self):
        pygame.mixer.music.stop()
        pygame.mixer.quit()


def resize_for_pi(frame):
    """Limit analysis and preview size to reduce Raspberry Pi workload."""
    height, width = frame.shape[:2]
    if width <= MAX_FRAME_WIDTH:
        return frame

    new_height = int(height * MAX_FRAME_WIDTH / width)
    return cv2.resize(
        frame,
        (MAX_FRAME_WIDTH, new_height),
        interpolation=cv2.INTER_AREA,
    )


def detect_emotion(frame):
    """Return (raw emotion, playlist category, face boxes) for one frame."""
    try:
        results = DeepFace.analyze(
            img_path=frame,
            actions=["emotion"],
            enforce_detection=True,
            detector_backend="opencv",
            silent=True,
        )
    except ValueError:
        # DeepFace raises when no face is found and enforce_detection is enabled.
        return None, None, []
    except Exception as error:
        print(f"Emotion analysis error: {error}")
        return None, None, []

    if isinstance(results, dict):
        results = [results]

    faces = []
    for result in results or []:
        if not isinstance(result, dict):
            continue

        raw_emotion = result.get("dominant_emotion")
        if not raw_emotion and isinstance(result.get("emotion"), dict):
            scores = result["emotion"]
            raw_emotion = max(scores, key=scores.get) if scores else None
        if not raw_emotion:
            continue

        region = result.get("region") or {}
        try:
            x = int(region.get("x", 0))
            y = int(region.get("y", 0))
            width = int(region.get("w", 0))
            height = int(region.get("h", 0))
        except (TypeError, ValueError):
            continue

        if width <= 0 or height <= 0:
            continue

        raw_emotion = str(raw_emotion).lower()
        faces.append({
            "emotion": raw_emotion,
            "playlist": EMOTION_TO_PLAYLIST.get(raw_emotion),
            "box": (x, y, width, height),
            "area": width * height,
        })

    if not faces:
        return None, None, []

    # For multiple faces, use the largest detected face to choose the playlist.
    largest_face = max(faces, key=lambda face: face["area"])
    return largest_face["emotion"], largest_face["playlist"], faces


def stable_playlist(history):
    """Return a playlist only when it wins at least two of the last three scans."""
    counts = Counter(category for category in history if category is not None)
    if not counts:
        return None

    highest_count = max(counts.values())
    if highest_count < 2:
        return None

    # If tied, prefer the category from the most recent matching scan.
    for category in reversed(history):
        if category is not None and counts[category] == highest_count:
            return category
    return None


def draw_overlay(frame, faces, detected_emotion, player):
    for face in faces:
        x, y, width, height = face["box"]
        color = (0, 220, 0) if face["playlist"] else (0, 180, 255)
        cv2.rectangle(frame, (x, y), (x + width, y + height), color, 2)
        label = face["emotion"]
        cv2.putText(
            frame,
            label,
            (x, max(20, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2,
        )

    emotion_text = detected_emotion or "No face detected"
    playlist_text = player.emotion or "Waiting"
    song_text = player.current_song.name if player.current_song else "None"

    cv2.putText(frame, f"Detected: {emotion_text}", (12, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    cv2.putText(frame, f"Playlist: {playlist_text}", (12, 52),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    cv2.putText(frame, f"Song: {song_text}", (12, 79),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
    cv2.putText(frame, "Q: quit   S: skip   P: pause/resume", (12, frame.shape[0] - 14),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)


def main():
    camera = cv2.VideoCapture(CAMERA_INDEX)
    if not camera.isOpened():
        camera.release()
        raise RuntimeError(
            f"Could not open webcam {CAMERA_INDEX}. Check the camera connection "
            "or change CAMERA_INDEX in the script."
        )

    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    try:
        pygame.mixer.init()
    except pygame.error as error:
        camera.release()
        raise RuntimeError(f"Could not initialize audio output: {error}") from error

    player = MusicPlayer()
    recent_playlists = deque(maxlen=3)
    last_detected_emotion = None
    last_faces = []
    next_analysis_time = 0.0

    print("Emotion-based music player started.")
    print("Press Q to quit, S to skip, or P to pause/resume.")

    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                print("Could not read a frame from the webcam.")
                break

            frame = resize_for_pi(frame)
            now = time.monotonic()

            # DeepFace inference is intentionally periodic rather than per frame.
            if now >= next_analysis_time:
                last_detected_emotion, category, last_faces = detect_emotion(frame)

                recent_playlists.append(category)
                selected = stable_playlist(recent_playlists)
                if selected and selected != player.emotion:
                    player.select_emotion(selected)

                next_analysis_time = time.monotonic() + ANALYSIS_INTERVAL_SECONDS

            player.update()
            draw_overlay(frame, last_faces, last_detected_emotion, player)
            cv2.imshow("Emotion-Based Music Player", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("s"):
                player.skip()
            elif key == ord("p"):
                player.toggle_pause()

            time.sleep(0.005)
    finally:
        camera.release()
        cv2.destroyAllWindows()
        player.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"Startup error: {error}")



