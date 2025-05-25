import requests
import base64
import mimetypes
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import os
from typing import List, Dict, Union

class PixabayImageSearchTool:
    """
    A tool for AI Agents to search and retrieve image links from Pixabay.
    It wraps the image search functionality as a LongRunningFunctionTool for ADK.
    """
    def __init__(self):
        """
        Initializes the Pixabay Image Search Tool with your Pixabay API key.

        Args:
            api_key: Your personal Pixabay API key. It is strongly
                     recommended to load this from an environment variable
                     (e.g., `os.getenv("PIXABAY_API_KEY")`) or a secure
                     configuration system, rather than hardcoding it.
        """
        api_key = os.getenv("PIXABAY_API_KEY")

        if not api_key:
            raise ValueError("Pixabay API key cannot be empty. Please provide a valid key.")
        self.api_key = api_key
        # Wrap the internal search function using ADK's LongRunningFunctionTool
        self.tool = self._search_images_internal
        # Propagate docstring and type hints from the internal function to the tool object
        self.__doc__ = self.tool.__doc__
        self.__annotations__ = self.tool.__annotations__

    def _search_images_internal(
        self,
        queries_info: List[Dict[str, Union[str, int]]],
    ) -> Dict[str, List[str]]:
        """
        Searches for images on Pixabay based on a list of queries.

        Args:
            queries_info: A list of dictionaries, where each dictionary represents
                          a search request and must contain:
                - "query" (str): The search term (e.g., "yellow flowers").
                - "num_images" (int): The desired number of image links to retrieve.
                                      (Will fetch up to 500 images per query due to Pixabay API limits).
            tool_context: An optional context object provided by the ADK framework.

        Returns:
            A dictionary where:
            - Keys are the original search queries (str).
            - Values are lists of `webformatURL` image links (List[str]).

            If no images are found for a query, that query will not be included
            in the output dictionary. If fewer images are found than requested,
            all available images will be returned for that query.
        """
        base_url = "https://pixabay.com/api/"
        results: Dict[str, List[str]] = {}

        for item in queries_info:
            query = item.get("query")
            # Default to 1 image if 'num_images' is not specified or invalid
            num_images = int(item.get("num_images", 1)) 

            if not query:
                print(f"Warning: Skipping search item due to missing or empty 'query'. Item: {item}")
                continue
            if num_images <= 0:
                print(f"Warning: Skipping search item for query '{query}' as 'num_images' is not positive.")
                continue

            images_for_current_query: List[str] = []
            
            # Pixabay API limits `per_page` to 200 and `totalHits` (accessible images) to 500 per query.
            # We will fetch up to 500 images or the requested `num_images`, whichever is smaller.
            effective_num_images_to_fetch = min(num_images, 500)
            
            # Calculate the number of API pages (requests) needed, max 200 images per page
            # Using ceiling division to ensure we cover all images if not a multiple of 200.
            pages_to_fetch = (effective_num_images_to_fetch + 199) // 200 

            for page_num in range(1, pages_to_fetch + 1):
                # Stop if we've already collected enough images
                if len(images_for_current_query) >= effective_num_images_to_fetch:
                    break

                # Calculate how many images to request on the current page
                remaining_to_fetch = effective_num_images_to_fetch - len(images_for_current_query)
                current_per_page = min(200, remaining_to_fetch) # Max 200 per page

                if current_per_page <= 0: # Should not happen if logic is correct, but as a safeguard
                    break

                params = {
                    "key": self.api_key,
                    "q": query,
                    "image_type": "photo", # Filtering for photos as per common use case
                    "per_page": current_per_page,
                    "page": page_num,
                    "safesearch": "true" # Ensure family-friendly results by default
                }

                try:
                    response = requests.get(base_url, params=params)
                    response.raise_for_status()  # Raises HTTPError for 4xx/5xx responses
                    data = response.json()

                    if "hits" in data:
                        for hit in data["hits"]:
                            # The documentation suggests `webformatURL` for temporary display of search results.
                            if "webformatURL" in hit:
                                images_for_current_query.append(hit["webformatURL"])
                                # Stop once we have gathered the required number of images
                                if len(images_for_current_query) >= effective_num_images_to_fetch:
                                    break
                    else:
                        print(f"No 'hits' found in response for query '{query}' (Page {page_num}). Response: {data}")

                except requests.exceptions.HTTPError as e:
                    print(f"HTTP Error for query '{query}' (Page {page_num}): Status {e.response.status_code} - {e.response.text}")
                    # Common errors: 400 (Bad Request), 429 (Too Many Requests), 500 (Internal Server Error)
                    # For rate limits (429), the tool might need a retry mechanism with backoff.
                    break # Stop processing this query on HTTP error
                except requests.exceptions.RequestException as e:
                    print(f"Network or request error for query '{query}' (Page {page_num}): {e}")
                    break # Stop processing this query on network error
                except ValueError: # JSONDecodeError is a subclass of ValueError
                    print(f"Failed to decode JSON response for query '{query}' (Page {page_num}).")
                    break # Stop processing this query on invalid JSON
                except Exception as e:
                    print(f"An unexpected error occurred for query '{query}' (Page {page_num}): {e}")
                    break

            # Only add the query to results if images were found
            if images_for_current_query:
                results[query] = images_for_current_query

        return results


def fetch_image_as_base64(src):
    """Fetch an image from a URL or local path and return it as a base64 data URI."""
    try:
        if src.startswith("data:"):
            return src  # already base64

        if src.startswith("http"):
            response = requests.get(src, timeout=5)
            response.raise_for_status()
            content = response.content
            mime = response.headers.get("Content-Type", mimetypes.guess_type(src)[0])
        else:
            with open(src, 'rb') as f:
                content = f.read()
            mime = mimetypes.guess_type(src)[0] or 'application/octet-stream'

        encoded = base64.b64encode(content).decode('utf-8')
        return f"data:{mime};base64,{encoded}"
    except Exception as e:
        print(f"[!] Could not convert image {src}: {e}")
        return src

def replace_svg_image_links_with_base64(svg_content):
    """Replaces <image> tags' href or xlink:href in SVG content with base64 image data."""
    soup = BeautifulSoup(svg_content, 'lxml-xml')  # 'xml' parser preserves SVG structure
    image_tags = soup.find_all('image')

    for tag in image_tags:
        href = tag.get('xlink:href') or tag.get('href')
        if href:
            data_uri = fetch_image_as_base64(href)
            if tag.has_attr('xlink:href'):
                tag['xlink:href'] = data_uri
            else:
                tag['href'] = data_uri

    return str(soup)
