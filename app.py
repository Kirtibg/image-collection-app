import streamlit as st
import streamlit.components.v1 as components
import os
import json
import pandas as pd
from streamlit_js_eval import streamlit_js_eval
from PIL import Image
import io
import base64
import time
from io import BytesIO
from io import StringIO
from github import Github
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import requests
import uuid
import re

# New imports for city search + interactive map
import geonamescache
import folium
from streamlit_folium import st_folium

country = 'India'
continent = 'Asia'
country_code = 'IN'  # ISO code used to filter GeoNames cities

firebase_secrets = st.secrets["firebase"]
token = firebase_secrets["github_token"]
repo_name = firebase_secrets["github_repo"]
owner, repo_name = repo_name.split('/')
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
# Initialize Firebase (only if not already initialized)
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

# Get Firestore client
db = firestore.client()


# ---- CITY DATA (GeoNames via geonamescache) ----
@st.cache_data(show_spinner=False)
def load_cities(cc):
    """Return {city_name: (lat, lng)} for the given country code, sorted by name."""
    gc = geonamescache.GeonamesCache()
    cities = gc.get_cities()
    data = {}
    for c in cities.values():
        if c.get("countrycode") == cc:
            name = c["name"]
            # keep the first occurrence's coordinates if names duplicate
            if name not in data:
                data[name] = (float(c["latitude"]), float(c["longitude"]))
    return dict(sorted(data.items(), key=lambda x: x[0].lower()))

CITIES = load_cities(country_code)
# Rough country centroid used when no city is selected / city is typed manually
COUNTRY_CENTER = (22.0, 79.0)
COUNTRY_ZOOM = 5


# ---- SESSION STATE ----
if "index" not in st.session_state:
    st.session_state.index = 0
    st.session_state.responses = []

if "prolific_id" not in st.session_state:
    st.session_state.prolific_id = None


# ---- UI ----
st.title(f"Image Collection from {country}")
st.markdown(f"""
We are collecting a dataset of images from **{country}** to assess the knowledge of modern-day AI technologies about surroundings within the country. With your consent, we request you to upload photos that you have taken but have **not shared online**.

Following are the instructions for the same.

**What kind of images to upload**:

- Photos should depict a variety of surroundings within {country}.
- Avoid uploading duplicate/near-duplicate photos that you have already uploaded.
- Ensure the images are clear and well-lit.
- Outdoor scenes are preferred.
- Avoid uploading images with identifiable faces and license plates to protect privacy.

**Image Requirements**:

-   All images must be from **within {country}**.
-   Do **not** upload images already posted on social media.
-   Try to upload images that represent diverse locations or settings.

**What to do:**
1.  **Upload 10 images**, one at a time.
2.  For each image:
    -   Wait for the photo to appear on screen.
    -   **Confirm** that the photo is outdoors and contains no identifiable faces.
    -   **Rate** how clearly the photo suggests it was taken in {country}, and (if it does) **list the clues** that helped you decide.
    -   **Rate** the popularity of the location captured.
    -   **Select the city** where the photo was taken, and **pin the exact spot** on the map.
    -   **Click** "Submit and Next" to move on.

After you upload a photo, wait for it to appear on screen before answering the questions. The entire activity would take around **20 minutes**. 
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
    # --- MAIN APP LOGIC (runs only after Prolific ID is submitted) ---
    if st.session_state.index < 10:
        idx = st.session_state.index

        uploaded_file = st.file_uploader(
            f"Upload image {idx + 1}",
            type=["jpg", "jpeg", "png"],
            key=f"file_{idx}",
        )

        if uploaded_file:
            # getvalue() is non-destructive, so reruns (e.g. from the map) keep the bytes
            file_bytes = uploaded_file.getvalue()
            if len(file_bytes) < 100:
                st.error("⚠️ File seems too small. Possible read error.")
            encoded_content = base64.b64encode(file_bytes).decode("utf-8")
            image = Image.open(BytesIO(file_bytes))
            st.image(image, use_container_width=True)

            # ---- (4) Confirmations, immediately after the image ----
            st.markdown("**Before answering, please confirm the following:**")
            confirm_outdoor = st.checkbox(
                "I confirm this is an outdoor photo (it was not taken indoors).",
                key=f"outdoor_{idx}",
            )
            confirm_faces = st.checkbox(
                "I confirm this photo does not contain any identifiable faces or readable license plates.",
                key=f"faces_{idx}",
            )

            # ---- (5) Visual cues rating ----
            clue_text = None
            rating = st.radio(
                f"**To what extent does this image contain visual cues (e.g., local architecture, language, or scenery) that identify it as being from {country}?**",
                options=["Choose an option", 0, 1, 2, 3],
                format_func=lambda x: f"{'No evidence at all' if x==0 else f'A few features that are shared by multiple countries within {continent}, but not fully specific to {country}' if x==2 else f'Enough evidence specific to {country}' if x==3 else f'There are visual indications like architectural style, vegetations, etc, but I do not know if they are specific to {country} or {continent}' if x==1 else ''}",
                index=0,
                key=f"rating_{idx}",
            )

            # Show the hints box for anything OTHER than "No evidence at all" (i.e. 1, 2, 3)
            if rating in [1, 2, 3]:
                clue_text = st.text_area(
                    "Please describe **all** the visual clues in this photo that helped you identify the location"
                    "— for example architecture, signage or language, vehicles, vegetation, clothing, terrain, etc."
                    "A more detailed answer helps us a lot.",
                    height=140,
                    key=f"clues_{idx}",
                )
                word_count = len((clue_text or "").split())
                st.caption(f"{word_count}/200 words — please keep your answer under 200 words.")
                if word_count > 200:
                    st.warning("⚠️ Your answer is over 200 words. Please shorten it before submitting.")

            # ---- (6) Popularity (unchanged) ----
            popularity = st.radio(
                "**How would you rate the popularity of the location depicted in the photo you uploaded?**",
                options=["Choose an option", 0, 1, 2],
                format_func=lambda x: f"{'The location depicts only a regular scene' if x==0 else f'The location may be locally popular, but not country-wide' if x==1 else f'The location is popular country-wide' if x==2 else 'Choose an option'}",
                index=0,
                key=f"pop_{idx}",
            )

            # ---- (7) City search dropdown (GeoNames) with manual fallback ----
            st.markdown(f"**Where in {country} was this photo taken?**")
            SELECT_PLACEHOLDER = "Select a city…"
            OTHER_OPTION = "Other — my city/town is not in the list (I'll type it)"
            city_options = [SELECT_PLACEHOLDER] + list(CITIES.keys()) + [OTHER_OPTION]
            city_choice = st.selectbox(
                "Search and select the nearest city/town:",
                options=city_options,
                index=0,
                key=f"city_{idx}",
            )

            city_name = None
            map_center = COUNTRY_CENTER
            map_zoom = COUNTRY_ZOOM

            if city_choice == OTHER_OPTION:
                typed = st.text_input("Type the city / town name:", key=f"city_other_{idx}")
                city_name = typed.strip() if typed and typed.strip() else None
            elif city_choice != SELECT_PLACEHOLDER:
                city_name = city_choice
                map_center = CITIES[city_choice]
                map_zoom = 11

            # ---- (8) Optional interactive map to pin the exact spot ----
            st.markdown(
                "*(Optional)* Click on the map to drop a pin on the **exact** spot where the photo was taken. "
                "The map centers on the city you selected above."
            )
            coords_key = f"coords_{idx}"
            saved_coords = st.session_state.get(coords_key)

            fmap = folium.Map(location=map_center, zoom_start=map_zoom)
            if saved_coords:
                folium.Marker(
                    [saved_coords["lat"], saved_coords["lng"]],
                    tooltip="Selected location",
                ).add_to(fmap)

            map_data = st_folium(fmap, height=350, width=700, key=f"map_{idx}")
            if map_data and map_data.get("last_clicked"):
                lc = map_data["last_clicked"]
                st.session_state[coords_key] = {"lat": lc["lat"], "lng": lc["lng"]}

            coords = st.session_state.get(coords_key)
            if coords:
                st.success(
                    f"📍 Pinned location: Latitude {coords['lat']:.6f}, Longitude {coords['lng']:.6f}"
                )

            # ---- Submit ----
            if st.button("Submit and Next", key=f"submit_{idx}"):
                # Validation
                if not (confirm_outdoor and confirm_faces):
                    st.error("Please tick both confirmation boxes (outdoor photo, no identifiable faces/plates).")
                elif rating == "Choose an option":
                    st.error("Please answer the visual-cues question.")
                elif rating in [1, 2, 3] and (not clue_text or not clue_text.strip()):
                    st.error("Please describe the visual clues that helped you identify the location.")
                elif rating in [1, 2, 3] and len((clue_text or "").split()) > 200:
                    st.error("Your clues answer is over 200 words. Please shorten it.")
                elif popularity == "Choose an option":
                    st.error("Please answer the popularity question.")
                elif not city_name:
                    st.error("Please select (or type) the city where the photo was taken.")
                else:
                    # Upload image to GitHub
                    file_name = f"{st.session_state.prolific_id}_{idx}.png"
                    file_path = f"{country}_images/{file_name}"
                    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/contents/{file_path}"

                    headers = {
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    }
                    payload = {
                        "message": f"Upload {file_path}",
                        "content": encoded_content,
                        "branch": "main",
                    }

                    response = requests.put(api_url, headers=headers, json=payload)
                    if response.status_code in [200, 201]:
                        st.success("Image uploaded to GitHub successfully.")
                    else:
                        st.error(f"Upload failed: {response.status_code}")
                        st.text(response.json())

                    st.session_state.responses.append({
                        "name": st.session_state.prolific_id,
                        "birth_country": st.session_state.birth_country,
                        "residence": st.session_state.residence,
                        "privacy": st.session_state.privacy,
                        "image_url": file_path,
                        "rating": rating,
                        "city": city_name,
                        "coords": coords,  # may be None if they skipped the optional map pin
                        "popularity": popularity,
                        "clues": clue_text,
                    })

                    st.session_state.index += 1
                    st.rerun()
    else:
        doc_ref = db.collection("Image_procurement").document(st.session_state.prolific_id)
        doc_ref.set({
            "prolific_id": st.session_state.prolific_id,
            "birth_country": st.session_state.birth_country,
            "country_of_residence": st.session_state.residence,
            "privacy": st.session_state.privacy,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "responses": st.session_state.responses,
        })
        st.session_state.submitted_all = True
        st.success("Survey complete. Thank you!")
        st.write("Survey complete! Thank you.")
