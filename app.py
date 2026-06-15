import streamlit as st
import streamlit.components.v1 as components
import json
import base64
from io import BytesIO
from PIL import Image
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import requests
import zipfile
import tempfile
import pathlib
import geonamescache



firebase_secrets = st.secrets["firebase"]
token = firebase_secrets["github_token"]
repo_name = firebase_secrets["github_repo"]
owner, repo_name = repo_name.split('/')
maps_api_key = firebase_secrets["Maps_API_KEY"]

MIN_IMAGES = 10
MAX_IMAGES = 30

MIN_CITY_POP = 500


PROLIFIC_COMPLETION_CODE = firebase_secrets.get(
    "prolific_completion_code", "REPLACE_WITH_YOUR_PROLIFIC_CODE"
)

# country = 'India'
# continent = 'Asia'

gc = geonamescache.GeonamesCache()
_COUNTRIES = gc.get_countries()  # keyed by ISO-2 code: "DK", "NO", "IN", ...

CONTINENT_NAMES = {
    "AF": "Africa", "AS": "Asia", "EU": "Europe",
    "NA": "North America", "SA": "South America",
    "OC": "Oceania", "AN": "Antarctica",
}


COUNTRY_CENTERS = {
    "DK": (56.0, 9.5),    # Denmark
    "NO": (61.0, 9.0),    # southern Norway (where most people live)
    "IN": (22.0, 79.0),   # India
}

COUNTRY_CODE = (st.query_params.get("country") or "DK").upper()
_meta = _COUNTRIES.get(COUNTRY_CODE)
if _meta is None:
    st.error(f"Unknown country code '{COUNTRY_CODE}'. Add ?country=<ISO2> to the URL (e.g. ?country=DK).")
    st.stop()

country = _meta["name"]
continent = CONTINENT_NAMES.get(_meta["continentcode"], _meta["continentcode"])



# Convert secrets to dict
cred_dict = {
    "type": firebase_secrets["type"],
    "project_id": firebase_secrets["project_id"],
    "private_key_id": firebase_secrets["private_key_id"],
    "private_key": firebase_secrets["private_key"].replace("\\n", "\n"),
    "client_email": firebase_secrets["client_email"],
    "client_id": firebase_secrets["client_id"],
    "auth_uri": firebase_secrets["auth_uri"],
    "token_uri": firebase_secrets["token_uri"],
    "auth_provider_x509_cert_url": firebase_secrets["auth_provider_x509_cert_url"],
    "client_x509_cert_url": firebase_secrets["client_x509_cert_url"],
    "universe_domain": firebase_secrets["universe_domain"]
}
cred = credentials.Certificate(json.loads(json.dumps(cred_dict)))
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

# # Rough country centroid used as the map's default center
# COUNTRY_CENTER = (22.0, 79.0)


# ---- CITY LIST (full GeoNames per-country dump, with geonamescache fallback) ----
@st.cache_data(show_spinner="Loading the list of cities/towns…", ttl=24 * 3600)
def load_cities(cc):
    """Return {place_name: (lat, lng)} for the given country code, sorted by name.

    Uses GeoNames' full per-country dump (all populated places) so that small
    towns/villages are included. Falls back to the geonamescache bundled list
    (population > 15,000 only) if the download fails.
    """
    # Feature codes we treat as "a place a participant might name".
    KEEP_CODES = {
        "PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLA5",
        "PPLC", "PPLG", "PPLL", "PPLS", "PPLX",
    }
    url = f"https://download.geonames.org/export/dump/{cc}.zip"
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        tmp = {}  # name -> (lat, lng, pop)
        with zipfile.ZipFile(BytesIO(resp.content)) as zf:
            with zf.open(f"{cc}.txt") as fh:
                for raw in fh:
                    parts = raw.decode("utf-8").rstrip("\n").split("\t")
                    if len(parts) < 15:
                        continue
                    # GeoNames columns: 1=name 4=lat 5=lng 6=feat_class 7=feat_code 14=pop
                    if parts[6] != "P" or parts[7] not in KEEP_CODES:
                        continue
                    name = parts[1]
                    try:
                        lat, lng = float(parts[4]), float(parts[5])
                    except ValueError:
                        continue
                    pop = int(parts[14]) if parts[14].isdigit() else 0
                    if pop < MIN_CITY_POP:
                        continue
                    # On duplicate names, keep the most populous one.
                    if name not in tmp or pop > tmp[name][2]:
                        tmp[name] = (lat, lng, pop)
        if tmp:
            data = {k: (v[0], v[1]) for k, v in tmp.items()}
            return dict(sorted(data.items(), key=lambda x: x[0].lower()))
    except Exception:
        pass  # fall through to the bundled list below

    # ---- Fallback: geonamescache bundled cities (population > 15,000) ----
    gc = geonamescache.GeonamesCache()
    cities = gc.get_cities()
    data = {}
    for c in cities.values():
        if c.get("countrycode") == cc:
            name = c["name"]
            if name not in data:  # keep first occurrence if duplicate names
                data[name] = (float(c["latitude"]), float(c["longitude"]))
    return dict(sorted(data.items(), key=lambda x: x[0].lower()))

CITIES = load_cities(COUNTRY_CODE)
CITY_NAMES = list(CITIES.keys())



# Map center: explicit table, else mean of city coords, else (0,0)
if COUNTRY_CODE in COUNTRY_CENTERS:
    COUNTRY_CENTER = COUNTRY_CENTERS[COUNTRY_CODE]
elif CITIES:
    lats = [v[0] for v in CITIES.values()]
    lngs = [v[1] for v in CITIES.values()]
    COUNTRY_CENTER = (sum(lats) / len(lats), sum(lngs) / len(lngs))
else:
    COUNTRY_CENTER = (0.0, 0.0)



# ---- GOOGLE MAPS PICKER (optional, bidirectional custom component) ----
# Geocoding-based search (no Places API needed) + click/drag pin.
# Returns {lat, lng, address}. Map center/zoom are passed in as component args.
_GMAPS_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>
  body { margin: 0; font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
  .row { display: flex; gap: 6px; margin-bottom: 6px; }
  #search { flex: 1; box-sizing: border-box; padding: 9px 10px; font-size: 14px;
            border: 1px solid #ccc; border-radius: 6px; }
  #searchBtn { padding: 9px 14px; font-size: 14px; border: none; border-radius: 6px;
               background: #1a73e8; color: #fff; cursor: pointer; }
  #map { width: 100%; height: 320px; border-radius: 8px; }
  #info { font-size: 13px; padding: 6px 2px; color: #444; }
</style>
</head>
<body>
  <div class="row">
    <input id="search" type="text" placeholder="Search a place, then press Enter or Search…" />
    <button id="searchBtn">Search</button>
  </div>
  <div id="map"></div>
  <div id="info">Search above, or click/drag the marker to set the exact spot.</div>

<script>
  function _send(type, data) {
    window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, data), "*");
  }
  function setComponentValue(v) { _send("streamlit:setComponentValue", { value: v }); }
  function setFrameHeight(h)   { _send("streamlit:setFrameHeight", { height: h }); }
  function ready()            { _send("streamlit:componentReady", { apiVersion: 1 }); }

  var map, marker, geocoder, mapReady = false, pendingCenter = null, pendingZoom = null, lastCenterKey = null;
  var DEFAULT = { lat: __LAT__, lng: __LNG__ };

  function emit(latLng, address) {
    setComponentValue({ lat: latLng.lat(), lng: latLng.lng(), address: address || "" });
  }

  function placeMarker(latLng) {
    if (!marker) {
      marker = new google.maps.Marker({ position: latLng, map: map, draggable: true });
      marker.addListener("dragend", function (e) { reverseAndEmit(e.latLng); });
    } else {
      marker.setPosition(latLng);
    }
  }

  function reverseAndEmit(latLng) {
    geocoder.geocode({ location: latLng }, function (results, status) {
      var addr = (status === "OK" && results[0]) ? results[0].formatted_address : "";
      document.getElementById("info").textContent = addr
        ? ("Selected: " + addr)
        : ("Selected: " + latLng.lat().toFixed(6) + ", " + latLng.lng().toFixed(6));
      emit(latLng, addr);
    });
  }

  function doSearch() {
    var q = document.getElementById("search").value;
    if (!q) return;
    geocoder.geocode({ address: q, componentRestrictions: { country: "__CC__" } }, function (results, status) {
      if (status === "OK" && results[0]) {
        var loc = results[0].geometry.location;
        if (results[0].geometry.viewport) map.fitBounds(results[0].geometry.viewport);
        else { map.setCenter(loc); map.setZoom(15); }
        placeMarker(loc);
        document.getElementById("info").textContent = "Selected: " + results[0].formatted_address;
        emit(loc, results[0].formatted_address);
      } else {
        document.getElementById("info").textContent = "No results for that search.";
      }
    });
  }

  function applyCenter() {
    if (mapReady && pendingCenter) {
      map.setCenter(pendingCenter);
      if (pendingZoom) map.setZoom(pendingZoom);
    }
  }

  function onRender(event) {
    var args = (event.data && event.data.args) || {};
    if (args.center) {
      var key = args.center.lat + "," + args.center.lng + "," + args.zoom;
      if (key !== lastCenterKey) {  // only recenter when the chosen city changes
        lastCenterKey = key;
        pendingCenter = args.center;
        pendingZoom = args.zoom;
        applyCenter();
      }
    }
  }
  window.addEventListener("message", function (e) {
    if (e.data && e.data.type === "streamlit:render") onRender(e);
  });

  function initMap() {
    geocoder = new google.maps.Geocoder();
    map = new google.maps.Map(document.getElementById("map"), {
      center: DEFAULT, zoom: 5, streetViewControl: false, mapTypeControl: false
    });
    mapReady = true;
    applyCenter();

    map.addListener("click", function (e) { placeMarker(e.latLng); reverseAndEmit(e.latLng); });
    document.getElementById("searchBtn").addEventListener("click", doSearch);
    document.getElementById("search").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); doSearch(); }
    });
    setFrameHeight(430);
  }

  window.initMap = initMap;
  ready();
  setFrameHeight(430);
</script>
<script async src="https://maps.googleapis.com/maps/api/js?key=__API_KEY__&callback=initMap"></script>
</body>
</html>
"""

_COMPONENT_DIR = pathlib.Path(tempfile.gettempdir()) / f"gmaps_picker_{COUNTRY_CODE}"
_COMPONENT_DIR.mkdir(exist_ok=True)
_html = (_GMAPS_HTML_TEMPLATE
         .replace("__API_KEY__", maps_api_key)
         .replace("__CC__", COUNTRY_CODE)
         .replace("__LAT__", str(COUNTRY_CENTER[0]))
         .replace("__LNG__", str(COUNTRY_CENTER[1])))
(_COMPONENT_DIR / "index.html").write_text(_html, encoding="utf-8")

_gmaps_component = components.declare_component(f"gmaps_picker_{COUNTRY_CODE}", path=str(_COMPONENT_DIR))

def gmaps_picker(center, zoom, key):
    """center=(lat,lng). Returns {'lat','lng','address'} once a spot is set, else None."""
    return _gmaps_component(
        default=None,
        center={"lat": center[0], "lng": center[1]},
        zoom=zoom,
        key=key,
    )


# ---- GITHUB UPLOAD (overwrite-safe + clearer errors) ----
def upload_image_to_github(file_path, content_b64):
    """PUT an image to the repo. If the path already exists (e.g. the participant
    refreshed and restarted), GitHub needs the existing blob's sha to overwrite it."""
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    sha = None
    try:
        get_resp = requests.get(api_url, headers=headers, params={"ref": "main"}, timeout=30)
        if get_resp.status_code == 200:
            sha = get_resp.json().get("sha")
    except requests.RequestException:
        pass  # network hiccup on the existence check; the PUT below will report any real error

    payload = {
        "message": f"Upload {file_path}",
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha

    return requests.put(api_url, headers=headers, json=payload, timeout=60)


# ---- Radio label helpers ----
def fmt_rating(x):
    return {
        0: "No evidence at all",
        1: f"There are visual indications like architectural style, vegetation, etc., but I do not know if they are specific to {country} or {continent}",
        2: f"A few features that are shared by multiple countries within {continent}, but not fully specific to {country}",
        3: f"Enough evidence specific to {country}",
    }[x]

def fmt_pop(x):
    return {
        0: "The location depicts only a regular scene",
        1: "The location may be locally popular, but not country-wide",
        2: "The location is popular country-wide",
    }[x]


# ---- SESSION STATE ----
if "index" not in st.session_state:
    st.session_state.index = 0
    st.session_state.responses = []

if "prolific_id" not in st.session_state:
    st.session_state.prolific_id = None

if "finished" not in st.session_state:
    st.session_state.finished = False


# ---- UI ----
st.title(f"Image Collection from {country}")
st.markdown(f"""
We are collecting a dataset of images from **{country}** to assess the knowledge of modern-day AI technologies about surroundings within the country. With your consent, we request you to upload photos that you have taken but have **not shared online**.

Following are the instructions for the same.

**What kind of images to upload**:

- Photos should depict a variety of surroundings within {country}.
- Avoid uploading duplicate/near-duplicate photos that you have already uploaded.
- Ensure the images are clear and well-lit.
- Upload outdoor scenes only (no indoor photos).
- Avoid uploading images with identifiable faces to protect privacy.

**Image Requirements**:

-   All images must be from **within {country}**.
-   Do **not** upload images already posted on social media.
-   Try to upload images that represent diverse locations or settings.

**What to do:**
1.  **Upload at least {MIN_IMAGES} images**, one at a time. After the required {MIN_IMAGES}, you may *optionally* keep uploading up to **{MAX_IMAGES} images** in total.
2.  For each image:
    -   Wait for the photo to appear on screen.
    -   **Confirm** that the photo is outdoors and contains no identifiable faces.
    -   **Select** the city/town where the photo was taken (or choose **"Other"** to type it if it isn't listed).
    -   **Rate** how clearly the photo shows it was taken in {country}. If it shows such cues, **describe the clues** that would help someone identify the location (up to 200 words).
    -   **Rate** how popular/well-known the location is.
    -   **Pin the exact spot** where the image was taken on the map by searching for the location or clicking on it.
    -   **Click** "Submit and Next" to move on to the next upload.
3.  Once you've uploaded the required {MIN_IMAGES}, a **"Finish the survey"** button appears. Click it whenever you're done to receive your **Prolific completion code**.

This usually takes around **20 minutes**, but there's no strict deadline. After you upload a photo, wait for it to appear on screen before answering the questions.
""", unsafe_allow_html=True)

if not st.session_state.prolific_id:
    with st.form("prolific_form"):
        pid = st.text_input("Please enter your Prolific ID to begin:", max_chars=24)
        birth = st.text_input("Please enter your country of birth", max_chars=24)
        res = st.text_input("Please enter your country of residence", max_chars=24)
        privacy = st.radio(
            "Do you permit us to release your images publically as a dataset, or strictly use them for our research purpose?",
            options=["You can make them public", "Only use them for your research"],
        )
        read_instructions = st.checkbox("I have read and understood the instructions above.")
        submitted = st.form_submit_button("Submit")
        if submitted:
            if pid.strip() and birth.strip() and res.strip() and read_instructions:
                st.session_state.prolific_id = pid.strip()
                st.session_state.birth_country = birth.strip()
                st.session_state.residence = res.strip()
                st.session_state.privacy = privacy
                st.success("Thank you! You may now begin.")
                st.rerun()
            elif not read_instructions:
                st.error("Please confirm that you have read the instructions before continuing.")
            else:
                st.error("Please enter a valid Prolific ID, birth country and residence country.")
else:
    # --- MAIN APP LOGIC ---
    # The participant is "done" if they reached the max, or chose to finish
    # after meeting the minimum.
    done = st.session_state.finished or (st.session_state.index >= MAX_IMAGES)

    if not done:
        idx = st.session_state.index

        # ---- Progress + (after the required minimum) a Finish button ----
        if idx < MIN_IMAGES:
            st.progress(idx / MIN_IMAGES,
                        text=f"{idx} of {MIN_IMAGES} required images uploaded")
        else:
            st.progress(1.0, text=f"{idx} images uploaded — minimum of {MIN_IMAGES} reached ✅")
            st.info(
                f"You've uploaded the required {MIN_IMAGES} images. "
                f"You're welcome to add more (up to {MAX_IMAGES} total), or finish now."
            )
            if st.button("✅ I'm done — finish the survey and get my completion code"):
                st.session_state.finished = True
                st.rerun()

        uploaded_file = st.file_uploader(
            f"Upload image {idx + 1}" + ("" if idx < MIN_IMAGES else " (optional)"),
            type=["jpg", "jpeg", "png"],
            key=f"file_{idx}",
        )

        if uploaded_file:
            # getvalue() is non-destructive, so reruns (e.g. from the map) keep the bytes
            file_bytes = uploaded_file.getvalue()
            if len(file_bytes) < 100:
                st.error("File seems too small. Possible read error.")
            encoded_content = base64.b64encode(file_bytes).decode("utf-8")
            image = Image.open(BytesIO(file_bytes))
            st.image(image, use_container_width=True)

            # ---- Confirmations, immediately after the image ----
            st.markdown("**Before answering, please confirm the following:**")
            confirm_outdoor = st.checkbox(
                "I confirm this is an outdoor photo (it was not taken indoors).",
                key=f"outdoor_{idx}",
            )
            confirm_faces = st.checkbox(
                "I confirm this photo does not contain any identifiable faces.",
                key=f"faces_{idx}",
            )

            # ---- City (required) — right after the confirmations ----
            st.markdown(f"**Which city/town in {country} was this photo taken in?**")
            SELECT_PLACEHOLDER = "Select a city…"
            OTHER_OPTION = "✏️ Other — my city/town is NOT listed (type it myself)"
            city_options = [SELECT_PLACEHOLDER, OTHER_OPTION] + CITY_NAMES
            city_choice = st.selectbox(
                "Choose the city/town (type to search):",
                options=city_options,
                index=0,
                key=f"city_{idx}",
            )
            st.caption(
                "Can't find your city/town in the list? Pick **\"Other\"** (at the top) to type it in yourself."
            )

            city_name = None
            if city_choice == OTHER_OPTION:
                typed = st.text_input("Type the city/town name:", key=f"city_other_{idx}")
                city_name = typed.strip() if typed and typed.strip() else None
            elif city_choice != SELECT_PLACEHOLDER:
                city_name = city_choice

            # ---- Visual cues rating (no option pre-selected) ----
            clue_text = None
            rating = st.radio(
                f"**To what extent does this image contain visual cues (e.g., local architecture, language, or scenery) that identify it as being from {country}?**",
                options=[0, 1, 2, 3],
                format_func=fmt_rating,
                index=None,
                key=f"rating_{idx}",
            )

            # Show the hints box for anything OTHER than "No evidence at all"
            if rating in [1, 2, 3]:
                clue_text = st.text_area(
                    "Please describe **all** the visual clues in this photo that would help someone identify "
                    "where it was taken — for example architecture, signage or language, vehicles, vegetation, "
                    "clothing, terrain, etc. Please give a comprehensive answer in no more than 200 words.",
                    height=140,
                    key=f"clues_{idx}",
                )
                word_count = len((clue_text or "").split())
                st.caption(f"{word_count}/200 words")
                if word_count > 200:
                    st.warning("Your answer is over 200 words. Please shorten it before submitting.")

            # ---- Popularity (no option pre-selected) ----
            popularity = st.radio(
                "**How would you rate the popularity of the location depicted in the photo you uploaded?**",
                options=[0, 1, 2],
                format_func=fmt_pop,
                index=None,
                key=f"pop_{idx}",
            )

            # ---- Map (optional) — kept at the end, independent of the city field ----
            st.markdown("**Pin the exact spot on the map** (optional)")
            st.caption("Search for the place and/or click & drag the marker to the exact spot.")
            picked = gmaps_picker(center=COUNTRY_CENTER, zoom=5, key=f"gmaps_{idx}")
            if picked and "lat" in picked:
                addr = picked.get("address") or ""
                st.success(
                    f"📍 Pinned: {addr + '  ·  ' if addr else ''}"
                    f"{picked['lat']:.6f}, {picked['lng']:.6f}"
                )

            # ---- Submit ----
            if st.button("Submit and Next", key=f"submit_{idx}"):
                if not (confirm_outdoor and confirm_faces):
                    st.error("Please tick both confirmation boxes (outdoor photo, no identifiable faces).")
                elif rating is None:
                    st.error("Please answer the visual-cues question.")
                elif rating in [1, 2, 3] and (not clue_text or not clue_text.strip()):
                    st.error("Please describe the visual clues that would help someone identify the location.")
                elif rating in [1, 2, 3] and len((clue_text or "").split()) > 200:
                    st.error("Your clues answer is over 200 words. Please shorten it.")
                elif popularity is None:
                    st.error("Please answer the popularity question.")
                elif not city_name:
                    st.error("Please select (or type) the city where the photo was taken.")
                else:
                    file_name = f"{st.session_state.prolific_id}_{idx}.png"
                    file_path = f"{country}_images/{file_name}"
                    response = upload_image_to_github(file_path, encoded_content)
                    if response.status_code not in [200, 201]:
                        try:
                            detail = response.json().get("message", response.text)
                        except Exception:
                            detail = response.text
                        st.error(f"Upload failed ({response.status_code}): {detail}")
                        st.stop()  # don't record/advance on a failed upload

                    st.success("Image uploaded to GitHub successfully.")

                    pin_coords = (
                        {"lat": picked["lat"], "lng": picked["lng"]}
                        if (picked and "lat" in picked) else None
                    )
                    st.session_state.responses.append({
                        "name": st.session_state.prolific_id,
                        "birth_country": st.session_state.birth_country,
                        "residence": st.session_state.residence,
                        "privacy": st.session_state.privacy,
                        "image_url": file_path,
                        "rating": rating,
                        "city": city_name,
                        "place_address": picked.get("address") if picked else None,
                        "coords": pin_coords,  # None if they skipped the optional map pin
                        "popularity": popularity,
                        "clues": clue_text,
                    })

                    # Save progress to Firestore after EVERY image (merge into the same
                    # participant document), so partial / abandoned sessions are still captured.
                    db.collection("Image_procurement").document(st.session_state.prolific_id).set({
                        "prolific_id": st.session_state.prolific_id,
                        "birth_country": st.session_state.birth_country,
                        "country_of_residence": st.session_state.residence,
                        "privacy": st.session_state.privacy,
                        "timestamp": firestore.SERVER_TIMESTAMP,
                        "num_images": len(st.session_state.responses),
                        "completed": False,
                        "responses": st.session_state.responses,
                    }, merge=True)

                    st.session_state.index += 1
                    st.rerun()
    else:
        # Finished — either the participant reached MAX_IMAGES, or they chose to
        # stop after meeting MIN_IMAGES. Responses were already saved after each
        # image; just mark the session complete and show the completion code.
        db.collection("Image_procurement").document(st.session_state.prolific_id).set({
            "responses": st.session_state.responses,
            "num_images": len(st.session_state.responses),
            "completed": True,
            "completed_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        st.session_state.submitted_all = True

        st.success("Survey complete. Thank you!")
        st.markdown("### 🎉 You're all done!")
        st.write(
            f"You uploaded **{len(st.session_state.responses)}** images. "
            "Thank you for contributing to our research."
        )
        st.markdown("Your **Prolific completion code** is:")
        st.markdown(f"## `{PROLIFIC_COMPLETION_CODE}`")
        st.markdown(
            "Please copy this code back into Prolific to register your submission, "
            "or use the button below to return to Prolific automatically."
        )
        st.link_button(
            "↩️ Return to Prolific and submit",
            f"https://app.prolific.com/submissions/complete?cc={PROLIFIC_COMPLETION_CODE}",
        )
