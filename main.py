from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import time
import requests
import concurrent.futures
import asyncio
import logging
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import unicodedata
from newspaper import Article
from abc import ABC, abstractmethod

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

NEWS_API = '15186bd948c4451db8359e094ab45c18'
GNEWS_API = '13e512c12a5c7382dfd42e8be50c0e7a'

class CrawlRequest(BaseModel):
    queries: List[str]
    lang: Optional[str] = "en"
    max: Optional[int] = 10
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    content_type: Optional[str] = "html"  # Đổi default thành "html"
    country: Optional[List[str]] = ["us"]
    domains: Optional[List[str]] = None
    exclude_domains: Optional[List[str]] = None
    return_html: Optional[bool] = True
    return_images: Optional[bool] = True

@app.get("/")
async def root():
    return {"message": "News Crawler API is running", "status": "healthy"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "message": "Server is running"}

def generate_slug(title):
    if not title:
        return ""
    title = re.sub(r'<[^>]+>', '', title)
    slug = title.lower()
    slug = unicodedata.normalize('NFKD', slug).encode('ascii', 'ignore').decode('ascii')
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[-\s]+', '-', slug).strip('-')
    return slug[:100]

def clean_html_content(html):
    """Improved HTML cleaning with better preservation"""
    if not html:
        return ""
    
    try:
        soup = BeautifulSoup(html, 'lxml')
        
        # Remove unwanted tags but preserve content structure
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "iframe", "form"]):
            tag.decompose()
        
        # Remove inline styles and classes but keep structure
        for tag in soup.find_all():
            for attr in ['style', 'class', 'id', 'onclick', 'onload']:
                tag.attrs.pop(attr, None)
        
        # Only remove truly empty tags (no text and no images)
        empty_tags = soup.find_all(lambda tag: tag.name in ['div', 'span', 'p'] and 
                                  not tag.get_text(strip=True) and 
                                  not tag.find(['img', 'video', 'audio']))
        for tag in empty_tags:
            tag.decompose()
        
        # Get the body content if exists, otherwise full content
        body = soup.find('body')
        if body:
            content = str(body)
        else:
            content = str(soup)
        
        # Clean up whitespace
        content = re.sub(r'\s+', ' ', content)
        content = re.sub(r'>\s+<', '><', content)
        
        logger.info(f"HTML cleaned, length: {len(content)}")
        return content.strip()
        
    except Exception as e:
        logger.error(f"Error cleaning HTML: {e}")
        return html  # Return original if cleaning fails

def extract_images(article_url, html_content):
    if not html_content:
        return []
    
    try:
        soup = BeautifulSoup(html_content, 'lxml')
        images = []
        
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
            if src:
                # Convert to absolute URL
                if src.startswith('//'):
                    src = 'https:' + src
                elif src.startswith('/'):
                    base_url = f"{urlparse(article_url).scheme}://{urlparse(article_url).netloc}"
                    src = urljoin(base_url, src)
                elif not src.startswith('http'):
                    src = urljoin(article_url, src)
                
                # Filter out small/unwanted images
                if any(k in src.lower() for k in ['icon', 'logo', 'avatar', 'ad', 'banner']):
                    continue
                
                # Check if image has reasonable size attributes
                width = img.get('width')
                height = img.get('height')
                if width and height:
                    try:
                        w, h = int(width), int(height)
                        if w < 100 or h < 100:  # Skip small images
                            continue
                    except ValueError:
                        pass
                
                # Get alt text
                alt_text = img.get('alt', '').strip()
                
                # Create simplified image object (only url and alt)
                image_obj = {
                    'url': src,
                    'alt': alt_text
                }
                
                images.append(image_obj)
        
        return images
    except Exception as e:
        logger.error(f"Error extracting images: {e}")
        return []


class NewsAPIAdapter(ABC):
    @abstractmethod
    def get_articles(self, query, **kwargs): pass

    @abstractmethod
    def format_response(self, response): pass

class GNewsAdapter(NewsAPIAdapter):
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://gnews.io/api/v4/search"

    def get_articles(self, query, **kwargs):
        country = kwargs.get('country', ['us'])[0]
        params = {
            'q': query,
            'token': self.api_key,
            'lang': kwargs.get('lang', 'en'),
            'country': country,
            'max': kwargs.get('max', 10)
        }
        try:
            r = requests.get(self.base_url, params=params, timeout=30)
            r.raise_for_status()
            return self.format_response(r.json())
        except Exception as e:
            logger.error(f"[GNews ERROR]: {e}")
            return []

    def format_response(self, response):
        return [{
            'title': a.get('title', ''),
            'url': a.get('url'),
            'source': 'GNews',
            'publishedAt': a.get('publishedAt'),
            'description': a.get('description', ''),
            'image': a.get('image', '')
        } for a in response.get('articles', []) if a.get('url')]

class NewsAPIOrgAdapter(NewsAPIAdapter):
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://newsapi.org/v2/everything"

    def get_articles(self, query, **kwargs):
        params = {
            'q': query,
            'language': kwargs.get('lang', 'en'),
            'pageSize': kwargs.get('max', 10),
            'apiKey': self.api_key
        }
        if kwargs.get('from_date'):
            params['from'] = kwargs['from_date']
        if kwargs.get('to_date'):
            params['to'] = kwargs['to_date']
        if kwargs.get('domains'):
            params['domains'] = ",".join(kwargs['domains'])
        if kwargs.get('exclude_domains'):
            params['excludeDomains'] = ",".join(kwargs['exclude_domains'])
        try:
            r = requests.get(self.base_url, params=params, timeout=30)
            r.raise_for_status()
            return self.format_response(r.json())
        except Exception as e:
            logger.error(f"[NewsAPI ERROR]: {e}")
            return []

    def format_response(self, response):
        if response.get('status') != 'ok': 
            return []
        return [{
            'title': a.get('title', ''),
            'url': a.get('url'),
            'source': a.get('source', {}).get('name', 'NewsAPI'),
            'publishedAt': a.get('publishedAt'),
            'description': a.get('description', ''),
            'image': a.get('urlToImage', '')
        } for a in response.get('articles', []) if a.get('url')]

class ContentExtractor:
    def __init__(self, content_type='html', return_html=True, return_images=True):
        self.content_type = content_type
        self.return_html = return_html
        self.return_images = return_images

    def extract(self, article_data):
        url = article_data.get('url')
        logger.info(f"Extracting content from: {url}")
        
        try:
            # Download article
            article = Article(url)
            article.download()
            article.parse()
            
            # Debug: Log what we got
            logger.info(f"Article downloaded, HTML length: {len(article.html) if article.html else 0}")
            logger.info(f"Article text length: {len(article.text) if article.text else 0}")
            
            # Extract content based on type
            if self.content_type == 'html':
                if article.html:
                    content = clean_html_content(article.html)
                    logger.info(f"HTML content extracted, length: {len(content)}")
                else:
                    content = article.text or ""
                    logger.warning(f"No HTML found, using text instead for: {url}")
            else:
                content = article.text or ""
            
            # Extract images
            extracted_images = []
            
            if self.return_images:
                if article.html:
                    extracted_images = extract_images(url, article.html)
                    logger.info(f"Extracted {len(extracted_images)} images")
                else:
                    logger.warning(f"No HTML for image extraction: {url}")
            
            # Generate slug
            slug = generate_slug(article_data.get('title') or article_data.get('description') or url)
            
            result = {
                'title': article_data.get('title', ''),
                'slug': slug,
                'description': article_data.get('description', '') or article.meta_description or '',
                'content': content,
                'content_type': self.content_type,
                'url': url,
                'images': extracted_images if self.return_images else []
            }
            
            return result
            
        except Exception as e:
            logger.error(f"[Content ERROR for {url}]: {e}")
            return {
                'title': article_data.get('title', ''),
                'slug': generate_slug(article_data.get('title', '')),
                'description': article_data.get('description', ''),
                'content': '',
                'content_type': self.content_type,
                'url': url,
                'images': [],
                'error': str(e)  # Debug info
            }
class NewsAggregator:
    def __init__(self, max_workers=2):
        self.adapters = [GNewsAdapter(GNEWS_API), NewsAPIOrgAdapter(NEWS_API)]
        self.max_workers = max_workers

    def get_multiple_queries_articles(self, queries, **kwargs):
        all_articles = []
        exclude_domains = kwargs.get("exclude_domains") or []

        def fetch_query(query):
            results = []
            for adapter in self.adapters:
                try:
                    articles = adapter.get_articles(query, **kwargs)
                    results.extend(articles)
                    logger.info(f"Got {len(articles)} articles from {adapter.__class__.__name__} for query: {query}")
                except Exception as e:
                    logger.error(f"Error with {adapter.__class__.__name__}: {e}")
            return results

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(fetch_query, q) for q in queries]
            for f in concurrent.futures.as_completed(futures):
                try:
                    all_articles.extend(f.result())
                except Exception as e:
                    logger.error(f"Error processing query: {e}")

        unique_articles = self.remove_duplicates(all_articles)
        if exclude_domains:
            unique_articles = self.filter_excluded_domains(unique_articles, exclude_domains)
        
        logger.info(f"Total unique articles: {len(unique_articles)}")
        return unique_articles

    def remove_duplicates(self, articles):
        seen = set()
        result = []
        for a in articles:
            url = a.get("url")
            if url and url not in seen:
                seen.add(url)
                result.append(a)
        return result

    def filter_excluded_domains(self, articles, exclude_domains):
        return [a for a in articles if not any(domain in a.get("url", "") for domain in exclude_domains)]

@app.post("/crawl-news")
async def crawl_news(data: CrawlRequest):
    try:
        logger.info(f"[INFO] Received query: {data.queries}")
        logger.info(f"[INFO] Content type: {data.content_type}")
        
        if not data.queries or all(not q.strip() for q in data.queries):
            raise HTTPException(status_code=400, detail="At least one query is required")

        aggregator = NewsAggregator()
        domains = [d for d in data.domains or [] if d.strip()]
        exclude_domains = [d for d in data.exclude_domains or [] if d.strip()]
        country = data.country or ['us']

        loop = asyncio.get_event_loop()
        raw_articles = await loop.run_in_executor(None, lambda: aggregator.get_multiple_queries_articles(
            data.queries,
            lang=data.lang,
            max=data.max,
            country=country,
            from_date=data.from_date,
            to_date=data.to_date,
            domains=domains,
            exclude_domains=exclude_domains
        ))

        if not raw_articles:
            logger.warning("No articles found")
            return []

        raw_articles = raw_articles[:20]  # limit max 20 articles
        logger.info(f"Processing {len(raw_articles)} articles")
        
        extractor = ContentExtractor(
            content_type=data.content_type,
            return_html=data.return_html,
            return_images=data.return_images
        )
        
        enhanced_articles = await loop.run_in_executor(
            None, 
            lambda: [extractor.extract(a) for a in raw_articles]
        )
        
        logger.info(f"Enhanced {len(enhanced_articles)} articles")
        return enhanced_articles

    except Exception as e:
        logger.error(f"Error in crawl_news: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Global exception: {str(exc)}")
    return {"error": "Internal server error", "detail": str(exc)}

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
