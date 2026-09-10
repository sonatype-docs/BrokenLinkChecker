#!/usr/bin/env python3
"""
Enhanced Broken Link Checker with strict validation and folder categorization

Features:
- Multiple retry attempts with exponential backoff
- Stricter validation before marking as broken
- Folder/category extraction from source URLs
- Team-based Slack notifications
- CSV export with category column

Author: Claude Opus 4.6
"""

import os
import sys
import json
import time
import logging
import subprocess
import re
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Set, Tuple, Optional
from urllib.parse import urljoin, urlparse, unquote
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup


# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging(log_file: str = "link_checker_enhanced.log") -> logging.Logger:
    """Configure structured logging."""
    formatter = logging.Formatter(
        fmt='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger = logging.getLogger('enhanced_link_checker')
    logger.setLevel(logging.INFO)
    # Only add handler if not already configured (prevents duplicate handlers on re-import)
    if not logger.handlers:
        logger.addHandler(console_handler)

    return logger


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Load configuration from environment variables."""

    def __init__(self):
        env_file = Path(__file__).parent / '.env'
        if env_file.exists():
            self._load_env_file(env_file)

        self.docs_urls = [url.strip() for url in os.getenv('DOCS_URLS', '').split(',') if url.strip()]
        self.max_depth = int(os.getenv('MAX_CRAWL_DEPTH', '3'))  # NOTE: Not enforced for internal domain links
        self.max_pages = int(os.getenv('MAX_PAGES_PER_SITE', '100'))
        self.max_links = int(os.getenv('MAX_LINKS_TO_CHECK', '0'))
        self.follow_external = os.getenv('FOLLOW_EXTERNAL_LINKS', 'false').lower() == 'true'
        self.check_external = os.getenv('CHECK_EXTERNAL_LINKS', 'true').lower() == 'true'
        self.timeout = int(os.getenv('REQUEST_TIMEOUT_SECONDS', '10'))
        self.parallel_workers = int(os.getenv('PARALLEL_WORKERS', '20'))

        # Enhanced retry settings
        self.max_retries = int(os.getenv('MAX_RETRIES', '5'))  # More retries for verification
        self.retry_backoff = float(os.getenv('RETRY_BACKOFF', '2.0'))  # Exponential backoff
        self.verify_ssl_strict = os.getenv('VERIFY_SSL_STRICT', 'false').lower() == 'true'

        exclude_str = os.getenv('EXCLUDE_PATTERNS', '')
        self.exclude_patterns = [p.strip() for p in exclude_str.split(',') if p.strip()]

        self.enable_desktop = os.getenv('ENABLE_DESKTOP_NOTIFICATIONS', 'true').lower() == 'true'
        self.enable_slack = os.getenv('ENABLE_SLACK_NOTIFICATIONS', 'false').lower() == 'true'
        self.slack_webhook = os.getenv('SLACK_WEBHOOK_URL', '')
        self.slack_channel = os.getenv('SLACK_CHANNEL', '')
        self.notification_sound = os.getenv('NOTIFICATION_SOUND', 'Glass')

        self.min_broken_links_alert = int(os.getenv('MIN_BROKEN_LINKS_FOR_ALERT', '1'))
        self.notify_on_completion = os.getenv('NOTIFY_ON_COMPLETION', 'true').lower() == 'true'

        # GitHub Pages settings
        self.github_repo_name = os.getenv('GITHUB_REPO_NAME', 'broken-links-reports')
        self.github_username = os.getenv('GITHUB_USERNAME', '')
        self.enable_github_upload = os.getenv('ENABLE_GITHUB_UPLOAD', 'true').lower() == 'true'

        self.report_dir = Path(__file__).parent / 'reports'
        self.report_dir.mkdir(exist_ok=True)

        self.check_interval = int(os.getenv('CHECK_INTERVAL_HOURS', '24'))

    def _load_env_file(self, env_file: Path):
        """Load environment variables from .env file."""
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    value = value.split('#')[0].strip()
                    os.environ[key.strip()] = value

    def validate(self) -> bool:
        """Validate required configuration."""
        if not self.docs_urls:
            print("❌ No documentation URLs configured")
            return False
        return True


# ============================================================================
# HOMEPAGE FOLDER CATEGORIZATION
# ============================================================================

class CategoryExtractor:
    """Extract homepage folder category from URLs.

    Uses explicit page-to-product mapping (from actual Sonatype help breadcrumbs)
    for known pages, falling back to keyword-based matching for unknown ones.
    """

    # Explicit page→product mapping based on actual Sonatype help breadcrumbs.
    # Format: filename (without .html) → product name
    # This is the SOURCE OF TRUTH — supersedes keyword matching.
    PAGE_CATEGORY_MAP = {
        # Sonatype Lifecycle
        'best-practices-dashboard': 'Sonatype Lifecycle',
        'example-waiver-workflows': 'Sonatype Lifecycle',
        'docker-image-analysis': 'Sonatype Lifecycle',
        'hugging-face-model-analysis': 'Sonatype Lifecycle',
        'certificates-and-secure-connections': 'Sonatype Lifecycle',
        'logging-configuration': 'Sonatype Lifecycle',
        'webhooks-concepts--iq-server-and-slack-integration': 'Sonatype Lifecycle',
        'automated-pull-requests-in-npm': 'Sonatype Integrations',
        'pull-request-commenting': 'Sonatype Integrations',

        # Sonatype Repository Firewall
        'jfrog-artifactory-setup': 'Sonatype Repository Firewall',

        # Sonatype IQ Server
        'download-and-compatibility': 'Sonatype IQ Server',
        'release-specific-upgrade-instructions': 'Sonatype IQ Server',

        # Sonatype Nexus Repository (many of these WERE correctly classified)
        'bundle-development': 'Sonatype Nexus Repository',
        'create-a-composer-repository': 'Sonatype Nexus Repository',
        'create-a-helm-repository': 'Sonatype Nexus Repository',
        'create-an-oci-repository': 'Sonatype Nexus Repository',
        'download': 'Sonatype Nexus Repository',
        'nexus-repository-upgrade-paths': 'Sonatype Nexus Repository',
        'npm-security': 'Sonatype Nexus Repository',
        'resilient-nexus-repository-deployment-to-google-cloud': 'Sonatype Nexus Repository',
    }

    # Homepage folder mapping based on keywords in page names (fallback only)
    HOMEPAGE_FOLDERS = {
        'Sonatype Nexus Repository': [
            'nexus-repository',
            'nxrm',
            'repository-manager',
            'apt-repositories',
            'bower-repositories',
            'npm-',
            'maven-',
            'docker-',
            'pypi-',
            'nuget-',
            'helm-',
            'yum-',
            'go-',
            'conda-repositories',
            'conan-repositories',
            'cocoapods-repositories',
            'composer-repositories',
            'git-lfs-repositories',
            'p2-repositories',
            'r-repositories',
            'raw-repositories',
            'rubygems-repositories',
            'upgrading-nexus-repository',
            'deploy-nexus-repository',
            'resilient-nexus-repository',
            'configuration',
            'config',
            'logging',
            'authentication',
            'saml',
            'ldap',
            'ssl',
            'atlassian-crowd',
            'system-requirements',
            'database',
            'high-availability',
            'backup',
            'restore',
            'migration',
        ],

        'Sonatype IQ Server': [
            'iq-server',
            'iq-20',  # IQ release notes like iq-2023, iq-2024
            'iq-for-idea',
            'iq-for-eclipse',
            'iq-for-visual-studio',
            'download-and-compatibility',
            'java-compatibility-matrix',
            'cloud-deployments',
            'container-deployments',
        ],

        'Sonatype Lifecycle': [
            'lifecycle',
            'policy-management',
            'automated-pull-requests',
            'pull-request-commenting',
            'hugging-face-model-analysis',
            'ai-component-analysis',
            'log4j',
            'vulnerability',
            'cve',
            'find-and-fix',
            'best-practices-dashboard',
            'best-practices',
            'projected-time-savings',
            'application-level-breakdown',
            'roi',
            'success-metrics',
            'value-metrics',
            'executive-dashboard',
        ],

        'Sonatype Repository Firewall': [
            'firewall',
            'repository-firewall',
            'quarantine',
        ],

        'Sonatype Integrations': [
            'integration',
            'jenkins',
            'jira',
            'bamboo',
            'crowd',
            'gitlab',
            'github',
            'azure-devops',
            'teamcity',
            'fortify',
            'sonarqube',
            'notable-integrations-changes',
        ],

        'Sonatype SBOM Manager': [
            'sbom',
            'software-bill-of-materials',
            'sbom-manager',
        ],

        'Sonatype Developer': [
            'developer',
            'bundle-development',
            'plugin',
            'api',
            'sdk',
        ],

        'Sonatype Guide': [
            'guide',
            'getting-started',
            'overview',
        ],

        'Sonatype Platform Overview': [
            'platform-overview',
            'announcements',
            'sunsetting',
        ],
    }

    @staticmethod
    def extract_category(url: str, base_url: str = "") -> str:
        """Extract homepage folder category from URL.

        1. Explicit page mapping (source of truth from actual breadcrumbs)
        2. Keyword-based fallback for unknown pages
        3. Default to Nexus Repository

        Returns one of the 9 official Sonatype categories.
        """
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()
            filename = Path(path).stem

            # 1. Check explicit page mapping (source of truth)
            if filename in CategoryExtractor.PAGE_CATEGORY_MAP:
                return CategoryExtractor.PAGE_CATEGORY_MAP[filename]

            # 2. Fall back to keyword matching
            for folder, keywords in CategoryExtractor.HOMEPAGE_FOLDERS.items():
                for keyword in keywords:
                    if keyword in filename:
                        return folder

            # 3. Default
            return "Sonatype Nexus Repository"

        except Exception:
            return "Sonatype Nexus Repository"

    @staticmethod
    def detect_category_from_breadcrumb(url: str, session=None) -> Optional[str]:
        """Fetch a Sonatype help page and detect its parent product from breadcrumb.

        Returns the product name (e.g., 'Sonatype Lifecycle') or None if it can't be determined.
        Use this to auto-populate PAGE_CATEGORY_MAP for new pages.
        """
        try:
            import requests as _requests
            import re as _re
            if session is None:
                session = _requests.Session()
                session.headers.update({'User-Agent': 'Mozilla/5.0'})
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                return None
            m = _re.search(r'<ul class="breadcrumb">(.+?)</ul>', r.text, _re.DOTALL)
            if not m:
                return None
            items = _re.findall(
                r'<li[^>]*>\s*(?:<a[^>]*href="([^"]*)"[^>]*>)?\s*([^<]+?)\s*(?:</a>)?\s*</li>',
                m.group(1)
            )
            if len(items) >= 2:
                product = items[1][1].strip()
                # Validate it starts with 'Sonatype '
                if product.startswith('Sonatype '):
                    return product
            return None
        except Exception:
            return None

    @staticmethod
    def get_display_category(category: str) -> str:
        """Convert category to display-friendly name with emoji.

        Only supports the 9 official Sonatype categories.
        """
        emoji_map = {
            'Sonatype Platform Overview': '🏠',
            'Sonatype Guide': '📖',
            'Sonatype Nexus Repository': '📦',
            'Sonatype IQ Server': '🔍',
            'Sonatype Lifecycle': '🔄',
            'Sonatype Repository Firewall': '🛡️',
            'Sonatype SBOM Manager': '📋',
            'Sonatype Developer': '💻',
            'Sonatype Integrations': '🔗',
        }
        emoji = emoji_map.get(category, '📦')
        return f"{emoji} {category}"


# ============================================================================
# ENHANCED LINK CHECKER
# ============================================================================

class EnhancedLinkChecker:
    """Check links with strict validation and retry logic."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.session = self._create_session()
        self.checked_urls: Dict[str, Tuple[int, str]] = {}
        self.verification_cache: Dict[str, bool] = {}  # url -> is_truly_broken

    def _create_session(self) -> requests.Session:
        """Create a requests session with enhanced retry logic."""
        session = requests.Session()

        retry_strategy = Retry(
            total=self.config.max_retries,
            backoff_factor=self.config.retry_backoff,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )

        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0'
        })

        return session

    def check_link_with_retry(self, url: str, attempt: int = 1) -> Tuple[int, str]:
        """
        Check link with exponential backoff retry.

        Returns:
            Tuple of (status_code, error_message)
        """
        if url in self.checked_urls:
            return self.checked_urls[url]

        # URN links are invalid in HTML - return as broken immediately
        if url.startswith('urn:'):
            result = (0, 'Invalid URN link in HTML (not a valid web URL)')
            self.checked_urls[url] = result
            return result

        # 404-page.html links are suspicious broken references
        if '404-page.html' in url:
            result = (404, 'Link to 404 error page (likely broken reference)')
            self.checked_urls[url] = result
            return result

        try:
            # Try HEAD first
            response = self.session.head(
                url,
                timeout=self.config.timeout,
                allow_redirects=True,
                verify=True  # Verify SSL certificates
            )

            # If HEAD fails with client error, try GET
            if response.status_code >= 400:
                time.sleep(0.5)  # Small delay
                response = self.session.get(
                    url,
                    timeout=self.config.timeout,
                    allow_redirects=True,
                    stream=True,
                    verify=True
                )
                response.close()

            status = response.status_code
            error = "" if status < 400 else f"HTTP {status}"

            self.checked_urls[url] = (status, error)
            return (status, error)

        except requests.exceptions.Timeout:
            # Retry on timeout with exponential backoff
            if attempt < self.config.max_retries:
                wait_time = self.config.retry_backoff ** attempt
                self.logger.debug(f"Timeout on {url}, retrying in {wait_time}s (attempt {attempt}/{self.config.max_retries})")
                time.sleep(wait_time)
                return self.check_link_with_retry(url, attempt + 1)

            self.checked_urls[url] = (0, "Timeout (after retries)")
            return (0, "Timeout (after retries)")

        except requests.exceptions.SSLError as e:
            # SSL errors might be temporary, retry
            if attempt < self.config.max_retries:
                wait_time = self.config.retry_backoff ** attempt
                self.logger.debug(f"SSL Error on {url}, retrying in {wait_time}s (attempt {attempt}/{self.config.max_retries})")
                time.sleep(wait_time)
                return self.check_link_with_retry(url, attempt + 1)

            self.checked_urls[url] = (0, f"SSL Error (persistent)")
            return (0, f"SSL Error (persistent)")

        except requests.exceptions.ConnectionError as e:
            # Connection errors might be temporary, retry
            if attempt < self.config.max_retries:
                wait_time = self.config.retry_backoff ** attempt
                self.logger.debug(f"Connection error on {url}, retrying in {wait_time}s (attempt {attempt}/{self.config.max_retries})")
                time.sleep(wait_time)
                return self.check_link_with_retry(url, attempt + 1)

            error_str = str(e)
            # Check if it's a local URL (localhost, 127.0.0.1)
            if 'localhost' in url.lower() or '127.0.0.1' in url or '0.0.0.0' in url:
                self.checked_urls[url] = (0, "Local URL (expected)")
                return (0, "Local URL (expected)")

            self.checked_urls[url] = (0, "Connection Failed (persistent)")
            return (0, "Connection Failed (persistent)")

        except requests.exceptions.RequestException as e:
            error_msg = str(e)[:100]
            self.checked_urls[url] = (0, error_msg)
            return (0, error_msg)

    def is_truly_broken(self, url: str, status: int, error: str) -> bool:
        """
        Determine if a link is truly broken or a false positive.

        Returns True only for genuinely broken links.
        """
        # Test environment URLs leaked into production docs - always broken
        if url.startswith('https://help-test.sonatype.com/'):
            return True

        # URN schemes are invalid in HTML - report as broken
        if url.startswith('urn:'):
            return True

        # Links to 404-page.html are suspicious - likely broken references
        if '404-page.html' in url:
            return True

        # Skip ideas.sonatype.com connection pool errors
        if 'ideas.sonatype.com' in url and 'HTTPSConnectionPool' in error:
            return False

        # asciinema.org blocks automated requests but links are real - false positive
        if 'asciinema.org' in url and 'Connection Failed' in error:
            return False

        # 403 Forbidden - usually bot blocking, not truly broken
        if status == 403:
            return False

        # Local URLs are not broken, just not accessible
        if 'localhost' in url.lower() or '127.0.0.1' in url or 'Local URL' in error:
            return False

        # GPG/PGP key files from repo.sonatype.com - working as intended (trigger downloads)
        # These return 404 on HEAD but actually work (download on GET)
        # Pattern: repo.sonatype.com/*/pki/*/GPG-KEY-*.asc
        if 'repo.sonatype.com' in url and 'GPG-KEY' in url and url.endswith('.asc'):
            return False

        # 404, 500+ are truly broken
        if status == 404 or status >= 500:
            return True

        # Connection failures after retries are truly broken
        if 'Connection Failed (persistent)' in error:
            return True

        # Skip SSL errors - often false positives (certificate issues, not broken links)
        if 'SSL Error (persistent)' in error:
            return False

        # Persistent timeouts after retries
        if 'Timeout (after retries)' in error:
            return True

        return False


# ============================================================================
# WEB CRAWLER (reuse from original)
# ============================================================================

class WebCrawler:
    """Crawl documentation sites and extract links."""

    def __init__(self, config: Config, logger: logging.Logger, link_checker: EnhancedLinkChecker):
        self.config = config
        self.logger = logger
        self.link_checker = link_checker
        self.visited_pages: Set[str] = set()
        self.normalized_urls: Set[str] = set()  # Track normalized URLs to detect duplicates
        self.pages_to_crawl: List[Tuple[str, int]] = []
        self.all_links: Dict[str, Set[str]] = defaultdict(set)

    def normalize_url(self, url: str) -> str:
        """
        Normalize URL to detect duplicates.

        Converts various URL patterns to canonical form:
        - /en/en/page.html -> /en/page.html
        - /document/preview/page.html -> /en/page.html
        - Removes query strings and fragments
        """
        # Remove query strings and fragments for normalization
        base_url = url.split('?')[0].split('#')[0]

        # Normalize duplicate /en/en/ pattern
        base_url = base_url.replace('/en/en/', '/en/')

        # Normalize /document/preview/ to /en/
        base_url = base_url.replace('/document/preview/', '/en/')
        base_url = base_url.replace('/document/index.html', '/en/index.html')

        return base_url

    def should_crawl(self, url: str, base_domain: str) -> bool:
        """Check if URL should be crawled."""
        parsed = urlparse(url)

        if parsed.scheme not in ['http', 'https']:
            return False

        for pattern in self.config.exclude_patterns:
            if re.search(pattern, url):
                return False

        if not self.config.follow_external:
            if parsed.netloc != base_domain:
                return False

        # Check if normalized URL already visited (duplicate detection)
        normalized = self.normalize_url(url)
        if normalized in self.normalized_urls:
            self.logger.debug(f"Skipping duplicate: {url} (normalized: {normalized})")
            return False

        return True

    def extract_links(self, html: str, base_url: str) -> Dict[str, str]:
        """Extract all links from HTML with their surrounding text context.

        Returns:
            Dict mapping URL to context text (sentence/paragraph containing the link)
        """
        links_with_context = {}

        try:
            soup = BeautifulSoup(html, 'html.parser')

            for tag in soup.find_all('a', href=True):
                href = tag['href'].strip()

                # Skip malformed hrefs that look like placeholder text
                # (e.g., href="FastText" or href="TODO" - unresolved template variables)
                # A valid href either starts with a scheme, is a path (/), an anchor (#),
                # a mailto:, or is a proper protocol-relative URL (//)
                if href and not href.startswith(('http://', 'https://', 'ftp://', 'mailto:', 'tel:', '/', '#', './', '../', '//')):
                    # Suspicious: raw text-like href without protocol/slash
                    # Only accept if it looks like a domain (contains a dot)
                    if '.' not in href.split('?')[0].split('#')[0]:
                        self.logger.debug(f"Skipping malformed href '{href}' on {base_url}")
                        continue

                # Reject hrefs that resolve to obviously bad URLs like http://SingleWord
                absolute_url = urljoin(base_url, href)
                # Extract host and check if it's a plausible hostname
                try:
                    from urllib.parse import urlparse as _urlparse
                    parsed_url = _urlparse(absolute_url)
                    if parsed_url.scheme in ('http', 'https') and parsed_url.netloc:
                        # Hostname must contain a dot (domain.tld) OR be an IP
                        # Reject things like http://FastText, http://TODO, http://foo
                        host = parsed_url.hostname or ''
                        if '.' not in host and not host.replace('localhost', '').isdigit():
                            self.logger.debug(f"Skipping placeholder URL '{absolute_url}' from href '{href}' on {base_url}")
                            continue
                except Exception:
                    pass

                # Check if this is a 404-page.html link BEFORE stripping anchor
                is_404_page = '404-page.html' in absolute_url

                # Strip anchors for all URLs by default
                absolute_url_no_anchor = absolute_url.split('#')[0]

                # For 404-page.html, keep the full URL with anchor
                # For everything else, use URL without anchor
                if is_404_page:
                    final_url = absolute_url  # Keep anchor
                else:
                    final_url = absolute_url_no_anchor  # Strip anchor

                if final_url:
                    # Skip excluded patterns
                    excluded = False
                    for pattern in self.config.exclude_patterns:
                        if re.search(pattern, final_url):
                            excluded = True
                            break

                    if excluded:
                        continue

                    # Get the link text
                    link_text = tag.get_text(strip=True)

                    # Try to get surrounding context
                    context = self._extract_context(tag, link_text)

                    links_with_context[final_url] = context

        except Exception as e:
            self.logger.warning(f"Failed to parse HTML from {base_url}: {e}")

        return links_with_context

    def _extract_context(self, tag, link_text: str, max_length: int = 150) -> str:
        """Extract surrounding context text for a link.

        Args:
            tag: The anchor tag
            link_text: Text of the link itself
            max_length: Maximum context length

        Returns:
            Context string with the link highlighted
        """
        # Try to get parent paragraph or list item
        parent = tag.find_parent(['p', 'li', 'td', 'div'])

        if parent:
            # Get all text from parent
            text = parent.get_text(separator=' ', strip=True)

            # Truncate if too long
            if len(text) > max_length:
                # Try to find the link text position
                if link_text in text:
                    link_pos = text.find(link_text)
                    start = max(0, link_pos - 60)
                    end = min(len(text), link_pos + len(link_text) + 60)
                    text = '...' + text[start:end] + '...'
                else:
                    text = text[:max_length] + '...'

            return text
        else:
            # Fallback: just return link text
            return link_text if link_text else "(no context)"

    def crawl_site(self, start_url: str) -> Dict[str, Dict[str, str]]:
        """Crawl a documentation site starting from start_url.

        Returns:
            Dict mapping source_url to dict of {link_url: context_text}
        """
        self.logger.info(f"Starting crawl from: {start_url}")

        base_domain = urlparse(start_url).netloc
        self.pages_to_crawl = [(start_url, 0)]
        pages_crawled = 0

        # Crawl all internal pages (no limit), but don't follow external links
        while self.pages_to_crawl and (self.config.max_pages == 0 or pages_crawled < self.config.max_pages):
            current_url, depth = self.pages_to_crawl.pop(0)

            if current_url in self.visited_pages:
                continue

            # No depth limitation - crawl as deep as needed

            # Track normalized URL to prevent duplicates
            normalized = self.normalize_url(current_url)
            self.normalized_urls.add(normalized)

            self.visited_pages.add(current_url)
            pages_crawled += 1

            page_info = f"[{pages_crawled}]" if self.config.max_pages == 0 else f"[{pages_crawled}/{self.config.max_pages}]"
            self.logger.info(f"Crawling {page_info} depth={depth}: {current_url}")

            try:
                response = self.link_checker.session.get(
                    current_url,
                    timeout=self.config.timeout
                )

                if response.status_code != 200:
                    self.logger.warning(f"Failed to fetch {current_url}: HTTP {response.status_code}")
                    continue

                links_with_context = self.extract_links(response.text, current_url)
                self.all_links[current_url] = links_with_context

                for link in links_with_context.keys():
                    if link not in self.visited_pages and self.should_crawl(link, base_domain):
                        self.pages_to_crawl.append((link, depth + 1))

            except Exception as e:
                self.logger.error(f"Error crawling {current_url}: {e}")

        self.logger.info(f"✓ Crawl complete: {pages_crawled} pages visited")

        return self.all_links


# ============================================================================
# ENHANCED REPORT GENERATOR WITH CATEGORIES
# ============================================================================

class EnhancedReportGenerator:
    """Generate reports with category/folder information."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger

    def _normalize_url(self, url: str) -> str:
        """Normalize a URL to catch semantic duplicates.

        - Strip whitespace
        - Remove URL fragment (#foo) - same page, different anchor
        - Remove trailing slash
        """
        url = url.strip()
        url = url.split('#')[0]  # strip fragment
        if url.endswith('/') and url.count('/') > 3:
            # only strip trailing slash if it's not right after protocol
            url = url.rstrip('/')
        return url

    def _calculate_cumulative_fixes(self, current_broken_urls: Set[str]) -> int:
        """Count every unique URL that has EVER been reported as broken but is not currently broken.

        This gives an accurate 'total fixed' count that includes:
        - Links fixed then replaced by new broken ones
        - Links fixed across multiple scans

        Filters:
        - Only counts scans from June 1, 2026 onwards
        - Skips anomalous scans (>50 broken - misconfigurations)
        - Normalizes URLs (strip fragments, trailing slashes) to catch semantic dupes
        - Deduplicates against normalized current broken URLs

        Historical URLs that match current EXCLUDE_PATTERNS are still counted
        (they were legitimately broken; excluding them now retroactively is wrong).
        """
        import csv as _csv
        from datetime import datetime as _dt

        cutoff_date = _dt(2026, 6, 1)
        MAX_VALID_BROKEN = 50

        all_ever_broken: Set[str] = set()

        def _process_csv(csv_file: Path, url_col: str = 'Broken Link'):
            try:
                with open(csv_file, 'r', encoding='utf-8') as f:
                    reader = _csv.DictReader(f)
                    urls = []
                    for row in reader:
                        url = row.get(url_col) or row.get('URL') or ''
                        url = self._normalize_url(url)
                        if url:
                            urls.append(url)
                if len(urls) > MAX_VALID_BROKEN:
                    return
                for u in urls:
                    all_ever_broken.add(u)
            except (IOError, KeyError):
                pass

        # From reports directory
        report_dir = self.config.report_dir
        if report_dir.exists():
            for csv_file in report_dir.glob('broken_links_categorized_*.csv'):
                name = csv_file.stem
                date_part = name.replace('broken_links_categorized_', '')
                try:
                    dt = _dt.strptime(date_part, '%Y%m%d_%H%M%S')
                    if dt < cutoff_date:
                        continue
                    _process_csv(csv_file, 'Broken Link')
                except ValueError:
                    continue

        # Also check archive
        archive_dir = Path(__file__).parent / '.github_pages_repo' / 'archive'
        if archive_dir.exists():
            for csv_file in archive_dir.glob('report_*.csv'):
                name = csv_file.stem
                date_part = name.replace('report_', '')
                try:
                    dt = _dt.strptime(date_part, '%Y%m%d_%H%M%S')
                    if dt < cutoff_date:
                        continue
                    _process_csv(csv_file, 'Broken Link')
                except ValueError:
                    continue

        # Normalize current broken URLs for fair comparison
        normalized_current = {self._normalize_url(u) for u in current_broken_urls}

        # Fixed = URLs that were broken at some point but not currently broken
        fixed_urls = all_ever_broken - normalized_current
        return len(fixed_urls)

    def _gather_progress_data(self, current_count: int) -> Dict:
        """Gather historical broken link counts for progress chart.

        Progress tracking started on June 1, 2026 with 27 broken links baseline.
        """
        from datetime import datetime as _dt

        # Progress baseline: started June 1, 2026 with 27 broken links
        # Format: (date_label, broken_count)
        milestones = [
            ('Jun 1', 27),   # Starting baseline
            ('Jul 3', 27),
            ('Jul 31', 24),
            ('Aug 24', 24),
        ]

        # Try to read historical data from reports directory (only June 1+ data)
        try:
            history = {}
            cutoff_date = _dt(2026, 6, 1)

            report_dir = self.config.report_dir
            if report_dir.exists():
                for csv_file in report_dir.glob('broken_links_categorized_*.csv'):
                    name = csv_file.stem
                    date_part = name.replace('broken_links_categorized_', '')
                    try:
                        dt = _dt.strptime(date_part, '%Y%m%d_%H%M%S')
                        if dt < cutoff_date:
                            continue  # Skip data before June 1
                        with open(csv_file, 'r', encoding='utf-8') as f:
                            count = sum(1 for _ in f) - 1
                        # Keep only the latest scan per day, skip anomalies (>500)
                        if count < 500 and count > 0:
                            day_key = dt.strftime('%Y-%m-%d')
                            if day_key not in history or dt > history[day_key][0]:
                                history[day_key] = (dt, count)
                    except (ValueError, IOError):
                        continue

            # Also check archive
            archive_dir = Path(__file__).parent / '.github_pages_repo' / 'archive'
            if archive_dir.exists():
                for csv_file in archive_dir.glob('report_*.csv'):
                    name = csv_file.stem
                    date_part = name.replace('report_', '')
                    try:
                        dt = _dt.strptime(date_part, '%Y%m%d_%H%M%S')
                        if dt < cutoff_date:
                            continue  # Skip data before June 1
                        with open(csv_file, 'r', encoding='utf-8') as f:
                            count = sum(1 for _ in f) - 1
                        if count < 500 and count > 0:
                            day_key = dt.strftime('%Y-%m-%d')
                            if day_key not in history or dt > history[day_key][0]:
                                history[day_key] = (dt, count)
                    except (ValueError, IOError):
                        continue

            if history:
                # Start with Jun 1 baseline (27 broken)
                sorted_history = sorted(history.items())
                labels = ['Jun 1']
                counts = [27]

                for day_key, (dt, count) in sorted_history:
                    # Skip anomalous spikes (>30) - likely scan issues
                    if count > 30:
                        continue
                    label = dt.strftime('%b %d')
                    if label not in labels:
                        labels.append(label)
                        counts.append(count)

                # Add current scan
                today = _dt.now()
                today_label = today.strftime('%b %d')
                if labels[-1] != today_label:
                    labels.append(today_label)
                    counts.append(current_count)
                else:
                    counts[-1] = current_count

                return {'labels': labels, 'counts': counts}
        except Exception as e:
            self.logger.warning(f"Could not gather historical progress data: {e}")

        # Fallback to hardcoded milestones + current
        labels = [m[0] for m in milestones]
        counts = [m[1] for m in milestones]
        today_label = _dt.now().strftime('%b %d')
        if labels[-1] != today_label:
            labels.append(today_label)
            counts.append(current_count)
        else:
            counts[-1] = current_count
        return {'labels': labels, 'counts': counts}

    def generate_csv_with_categories(self, broken_links: List[Dict], base_url: str) -> Path:
        """Generate CSV with category column."""
        import csv

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_file = self.config.report_dir / f"broken_links_categorized_{timestamp}.csv"

        # Group by category
        by_category = defaultdict(list)
        for link in broken_links:
            category = CategoryExtractor.extract_category(link['source'], base_url)
            link['category'] = category
            by_category[category].append(link)

        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Homepage Folder', 'Source Page', 'Broken Link', 'Error', 'Status Code', 'Context'])

            # Write links grouped by category
            for category in sorted(by_category.keys()):
                for link in by_category[category]:
                    writer.writerow([
                        category,
                        link['source'],
                        link['url'],
                        link['error'],
                        link['status'],
                        link.get('context', '(no context)')
                    ])

        self.logger.info(f"✓ CSV with categories saved: {csv_file}")
        return csv_file

    def generate_category_summary(self, broken_links: List[Dict], base_url: str) -> Dict[str, List[Dict]]:
        """Generate summary grouped by category."""
        by_category = defaultdict(list)

        for link in broken_links:
            category = CategoryExtractor.extract_category(link['source'], base_url)
            by_category[category].append(link)

        return dict(by_category)

    def generate_html_report(self, broken_links: List[Dict], base_url: str) -> Path:
        """Generate HTML report with categories and context."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        html_file = self.config.report_dir / f"broken_links_report_{timestamp}.html"

        # Group by category
        by_category = defaultdict(list)
        for link in broken_links:
            category = CategoryExtractor.extract_category(link['source'], base_url)
            link['category'] = category
            by_category[category].append(link)

        # Build rows JSON for JS filtering
        import json as _json
        all_rows = []
        for category in sorted(by_category.keys()):
            display_cat = CategoryExtractor.get_display_category(category)
            for link in by_category[category]:
                error_type = link['error']
                if 'HTTP 404' in error_type or '404' in error_type:
                    error_label = '404'
                    error_class = 'e404'
                elif 'Connection Failed' in error_type or 'connection' in error_type.lower():
                    error_label = 'Connection'
                    error_class = 'econn'
                elif 'SSL' in error_type:
                    error_label = 'SSL'
                    error_class = 'essl'
                elif 'Timeout' in error_type:
                    error_label = 'Timeout'
                    error_class = 'etimeout'
                elif 'Invalid' in error_type or 'not a valid' in error_type.lower():
                    error_label = 'Invalid URL'
                    error_class = 'einvalid'
                elif 'Test environment' in error_type:
                    error_label = 'Test Env URL'
                    error_class = 'eother'
                else:
                    error_label = error_type[:18]
                    error_class = 'eother'
                source = link['source']
                url = link['url']
                context = (link.get('context') or '').strip()
                if len(context) > 120:
                    context = context[:117] + '...'
                all_rows.append({
                    'cat': display_cat,
                    'src': source,
                    'url': url,
                    'ctx': context,
                    'err': error_label,
                    'ecls': error_class,
                })

        rows_json = _json.dumps(all_rows)
        cats_json = _json.dumps(sorted([CategoryExtractor.get_display_category(c) for c in by_category.keys()]))
        scan_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        total_broken = len(broken_links)
        total_cats = len(by_category)

        # Gather historical progress data
        progress_data = self._gather_progress_data(total_broken)
        progress_json = _json.dumps(progress_data)
        initial_count = progress_data['counts'][0] if progress_data['counts'] else 27

        # Cumulative fix tracking: count every unique URL ever fixed
        # (includes links fixed and then replaced by new broken ones)
        current_broken_urls = set(link.get('url', '') for link in broken_links)
        links_fixed = self._calculate_cumulative_fixes(current_broken_urls)

        # Total ever broken = fixed + still broken
        total_ever_broken = links_fixed + total_broken
        improvement_pct = round((links_fixed / total_ever_broken * 100), 1) if total_ever_broken > 0 else 0

        # Category breakdown data for donut chart (simplified names, no emojis)
        cat_labels = []
        cat_counts = []
        for cat, links in sorted(by_category.items(), key=lambda x: -len(x[1])):
            simple_name = cat.replace('Sonatype ', '')
            cat_labels.append(simple_name)
            cat_counts.append(len(links))
        category_json = _json.dumps({'labels': cat_labels, 'counts': cat_counts})

        # Top pages needing attention
        page_counts = defaultdict(int)
        for link in broken_links:
            source = link.get('source', '')
            # Extract page name from URL
            page_name = source.replace('https://help.sonatype.com/en/', '').replace('.html', '')
            if not page_name:
                page_name = source
            page_counts[page_name] += 1

        top_pages = sorted(page_counts.items(), key=lambda x: -x[1])[:5]
        top_pages_html = ''
        for i, (page, count) in enumerate(top_pages, 1):
            display_name = page[:50] + '...' if len(page) > 50 else page
            top_pages_html += (
                f'<li class="top-pages-item">'
                f'<span class="top-pages-rank">{i}</span>'
                f'<span class="top-pages-name" title="{page}">{display_name}</span>'
                f'<span class="top-pages-count">{count}</span>'
                f'</li>'
            )
        if not top_pages_html:
            top_pages_html = '<li class="top-pages-item"><span class="top-pages-name" style="color:var(--gray-10)">No broken links found</span></li>'

        # Impact metrics — use actual scan count from most recent full scan
        # Fallback: 6001 (from last confirmed scan on Aug 24, 2026)
        total_links_scanned = getattr(self, '_last_scan_count', 6001)
        total_checked_display = f"{total_links_scanned:,}"
        healthy_pct = round(((total_links_scanned - total_broken) / total_links_scanned * 100), 2) if total_links_scanned > 0 else 0

        # Health score calculation (based on broken link ratio and trend)
        broken_ratio = (total_broken / 6000) * 100 if total_broken > 0 else 0
        trend_improving = total_broken <= initial_count

        if broken_ratio < 0.5 and trend_improving:
            health_grade = 'A'
            health_grade_class = 'a'
            health_status = 'Excellent'
        elif broken_ratio < 1.0 and trend_improving:
            health_grade = 'B'
            health_grade_class = 'b'
            health_status = 'Good'
        elif broken_ratio < 2.0:
            health_grade = 'C'
            health_grade_class = 'c'
            health_status = 'Fair'
        elif broken_ratio < 5.0:
            health_grade = 'D'
            health_grade_class = 'd'
            health_status = 'Needs Attention'
        else:
            health_grade = 'F'
            health_grade_class = 'f'
            health_status = 'Critical'

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Broken Links Report — {scan_ts}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
/* Sonatype Design System - Radix UI Colors */
:root {{
  /* Blue (Primary Brand) */
  --blue-1: #fbfdff;
  --blue-2: #f4f9ff;
  --blue-3: #e6f4ff;
  --blue-4: #d6efff;
  --blue-5: #c2e5ff;
  --blue-6: #a9d8ff;
  --blue-7: #86c6f8;
  --blue-8: #49a8f5;
  --blue-9: #1d9bf0;
  --blue-10: #0091e8;
  --blue-11: #006adc;
  --blue-12: #003366;

  /* Tomato (Accent Brand) */
  --tomato-1: #fffcfb;
  --tomato-2: #fff8f6;
  --tomato-3: #fff0ec;
  --tomato-4: #ffe6e0;
  --tomato-5: #fdd8cf;
  --tomato-6: #f5c5b6;
  --tomato-7: #ecaa96;
  --tomato-8: #e0856a;
  --tomato-9: #d95030;
  --tomato-10: #cd3d1d;
  --tomato-11: #be2c10;
  --tomato-12: #5c1c0b;

  /* Gray Scale */
  --gray-1: #fcfcfd;
  --gray-2: #f9f9fb;
  --gray-3: #f0f0f3;
  --gray-4: #e8e8ec;
  --gray-5: #e0e1e6;
  --gray-6: #d9d9e0;
  --gray-7: #cecede;
  --gray-8: #bbbbc6;
  --gray-9: #8b8b9a;
  --gray-10: #6e6e7c;
  --gray-11: #4a4a56;
  --gray-12: #1c1c24;

  /* Status Colors */
  --red-9: #e5484d;
  --red-11: #cd2b31;
  --green-9: #30a46c;
  --green-11: #18794e;
  --orange-9: #f76b15;
  --orange-11: #bd4b00;
  --yellow-9: #ffaa33;
  --yellow-11: #c68520;
}}

*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;background:var(--gray-2);color:var(--gray-12);min-height:100vh;line-height:1.5}}
a{{color:var(--blue-11);text-decoration:none;font-weight:500}}a:hover{{color:var(--blue-10);text-decoration:underline}}

/* ── top bar ── */
.topbar{{background:linear-gradient(135deg,var(--blue-11) 0%,#0052a3 100%);border-bottom:3px solid var(--tomato-9);padding:0 32px;display:flex;align-items:center;height:64px;gap:16px;position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,0.08)}}
.topbar-logo{{font-weight:700;font-size:16px;color:#fff;display:flex;align-items:center;gap:8px;letter-spacing:-0.02em}}
.topbar-logo span{{color:var(--tomato-9);font-size:20px}}
.topbar-meta{{color:rgba(255,255,255,0.9);font-size:12px;margin-left:auto;font-weight:500}}

/* ── layout ── */
.page{{max-width:1400px;margin:0 auto;padding:24px 32px}}

/* ── stat cards ── */
.stats{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
.stat-card{{background:#fff;border:1px solid var(--gray-4);border-radius:8px;padding:16px 24px;min-width:140px;flex:1;box-shadow:0 1px 3px rgba(0,0,0,0.04);transition:box-shadow 0.2s}}
.stat-card:hover{{box-shadow:0 4px 12px rgba(0,0,0,0.08)}}
.stat-card .val{{font-size:32px;font-weight:700;line-height:1.2;letter-spacing:-0.02em}}
.stat-card .lbl{{font-size:11px;color:var(--gray-10);margin-top:6px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600}}
.stat-card.red .val{{color:var(--red-11)}}
.stat-card.green .val{{color:var(--green-11)}}
.stat-card.blue .val{{color:var(--blue-11)}}
.stat-card.tomato{{border-left:4px solid var(--tomato-9)}}
.stat-card.tomato .val{{color:var(--tomato-11)}}

/* ── page heading ── */
.page-heading{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px}}
.page-heading h1{{font-size:28px;font-weight:700;color:var(--gray-12);letter-spacing:-0.03em}}
.page-sub{{font-size:13px;color:var(--gray-10);margin-top:6px}}

/* ── toolbar ── */
.toolbar{{background:#fff;border:1px solid var(--gray-4);border-radius:8px;padding:12px 16px;display:flex;gap:12px;align-items:center;margin-bottom:20px;flex-wrap:wrap;box-shadow:0 1px 3px rgba(0,0,0,0.04)}}
.toolbar input{{border:1px solid var(--gray-6);border-radius:6px;padding:8px 14px;font-size:13px;width:280px;outline:none;color:var(--gray-12);transition:all 0.2s}}
.toolbar input:focus{{border-color:var(--blue-8);box-shadow:0 0 0 3px rgba(29,155,240,0.15)}}
.toolbar select{{border:1px solid var(--gray-6);border-radius:6px;padding:8px 14px;font-size:13px;color:var(--gray-12);background:#fff;outline:none;cursor:pointer;transition:all 0.2s}}
.toolbar select:focus{{border-color:var(--blue-8);box-shadow:0 0 0 3px rgba(29,155,240,0.15)}}
.toolbar .sep{{width:1px;height:28px;background:var(--gray-4)}}
.toolbar .count{{margin-left:auto;font-size:13px;color:var(--gray-10);font-weight:500}}
.btn-clear{{border:1px solid var(--gray-6);border-radius:6px;padding:8px 16px;font-size:13px;background:#fff;cursor:pointer;color:var(--gray-11);font-weight:500;transition:all 0.2s}}
.btn-clear:hover{{background:var(--gray-3);border-color:var(--gray-7)}}

/* ── table ── */
.tbl-wrap{{background:#fff;border:1px solid var(--gray-4);border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.04)}}
table{{width:100%;border-collapse:collapse}}
thead tr{{background:linear-gradient(to bottom,var(--gray-1),var(--gray-2));border-bottom:2px solid var(--gray-5)}}
th{{padding:10px 14px;text-align:left;font-size:11px;font-weight:700;color:var(--gray-11);text-transform:uppercase;letter-spacing:0.5px;white-space:nowrap}}
td{{padding:10px 14px;border-bottom:1px solid var(--gray-3);vertical-align:top;font-size:13px}}
tbody tr:last-child td{{border-bottom:none}}
tbody tr:hover td{{background:var(--blue-2)}}
.col-cat{{width:14%}}
.col-src{{width:20%}}
.col-url{{width:32%}}
.col-ctx{{width:24%}}
.col-err{{width:10%}}

.cat-pill{{display:inline-block;padding:4px 12px;border-radius:12px;font-size:11px;font-weight:600;background:linear-gradient(135deg,var(--blue-3),var(--blue-2));color:var(--blue-12);border:1px solid var(--blue-4)}}
.src-link{{color:var(--blue-11);font-size:12px;word-break:break-all;font-weight:500}}
.broken-url{{font-family:'SF Mono',Consolas,Monaco,monospace;font-size:12px;color:var(--red-11);word-break:break-all;background:var(--gray-1);padding:2px 6px;border-radius:4px;display:inline-block}}
.ctx-text{{color:var(--gray-10);font-size:12px;line-height:1.5}}

.badge{{display:inline-block;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;white-space:nowrap;letter-spacing:0.3px}}
.e404{{background:linear-gradient(135deg,#fee2e2,#feecea);color:var(--red-11);border:1px solid #fecaca}}
.econn{{background:linear-gradient(135deg,#fef3c7,#fef0d7);color:var(--orange-11);border:1px solid #fed7aa}}
.essl{{background:linear-gradient(135deg,#d1fae5,#d4f5e3);color:var(--green-11);border:1px solid #a7f3d0}}
.etimeout{{background:linear-gradient(135deg,#dbeafe,#e0f2fe);color:var(--blue-11);border:1px solid #bfdbfe}}
.einvalid{{background:linear-gradient(135deg,var(--gray-2),var(--gray-1));color:var(--gray-11);border:1px solid var(--gray-4)}}
.eother{{background:linear-gradient(135deg,var(--gray-2),var(--gray-1));color:var(--gray-11);border:1px solid var(--gray-4)}}

/* ── empty state ── */
.empty{{text-align:center;padding:80px 20px;color:var(--gray-10)}}
.empty .ico{{font-size:48px;margin-bottom:16px}}
.empty p{{font-size:15px;font-weight:500}}

/* ── impact section ── */
.impact-section{{background:#fff;border:1px solid var(--gray-4);border-radius:8px;padding:24px 28px;margin-bottom:24px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}}
.impact-title{{font-size:13px;font-weight:600;color:var(--gray-10);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:16px}}
.impact-grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}
.impact-item{{border-left:3px solid var(--blue-11);padding-left:16px}}
.impact-item-val{{font-size:24px;font-weight:700;color:var(--gray-12);letter-spacing:-0.02em;line-height:1.2}}
.impact-item-lbl{{font-size:12px;color:var(--gray-10);margin-top:4px;font-weight:500}}

/* ── insights grid (category + top pages) ── */
.insights-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px}}
.insight-card{{background:#fff;border:1px solid var(--gray-4);border-radius:8px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}}
.insight-title{{font-size:13px;font-weight:600;color:var(--gray-10);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:20px}}
.donut-wrap{{display:flex;align-items:center;gap:24px;height:220px}}
.donut-container{{position:relative;width:220px;height:220px;flex-shrink:0}}
.donut-center{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;pointer-events:none}}
.donut-center-val{{font-size:32px;font-weight:700;color:var(--gray-12);letter-spacing:-0.02em;line-height:1}}
.donut-center-lbl{{font-size:11px;color:var(--gray-10);text-transform:uppercase;letter-spacing:0.5px;font-weight:600;margin-top:4px}}
.donut-legend{{flex:1;list-style:none;padding:0;margin:0}}
.donut-legend-item{{display:flex;align-items:center;padding:6px 0;font-size:12px;color:var(--gray-11)}}
.donut-legend-dot{{width:10px;height:10px;border-radius:2px;margin-right:10px;flex-shrink:0}}
.donut-legend-name{{flex:1;font-weight:500}}
.donut-legend-count{{font-weight:600;color:var(--gray-12);margin-left:8px}}
.top-pages-list{{list-style:none;padding:0;margin:0}}
.top-pages-item{{display:flex;align-items:center;padding:10px 0;border-bottom:1px solid var(--gray-3)}}
.top-pages-item:last-child{{border-bottom:none}}
.top-pages-rank{{width:24px;height:24px;background:var(--gray-3);color:var(--gray-11);border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;margin-right:12px;flex-shrink:0}}
.top-pages-name{{flex:1;font-size:12px;color:var(--gray-12);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:'SF Mono',Consolas,monospace}}
.top-pages-count{{background:var(--gray-3);color:var(--gray-12);padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;margin-left:12px;flex-shrink:0}}

/* ── health score ── */
.health-score{{display:flex;align-items:center;justify-content:center;gap:16px;padding:14px 20px;border:1px solid var(--gray-4);border-radius:8px;background:#fff;margin-left:16px}}
.health-grade{{width:56px;height:56px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:32px;font-weight:700;color:#fff;letter-spacing:-0.02em}}
.health-grade.a{{background:var(--green-11)}}
.health-grade.b{{background:var(--blue-11)}}
.health-grade.c{{background:var(--orange-11)}}
.health-grade.d{{background:var(--tomato-11)}}
.health-grade.f{{background:var(--red-11)}}
.health-info{{display:flex;flex-direction:column}}
.health-label{{font-size:11px;color:var(--gray-10);text-transform:uppercase;letter-spacing:0.5px;font-weight:600}}
.health-detail{{font-size:14px;color:var(--gray-12);font-weight:600;margin-top:2px}}

@media (max-width: 768px) {{
  .insights-grid{{grid-template-columns:1fr}}
  .health-score{{margin-left:0;margin-top:12px}}
}}

/* ── progress section ── */
.progress-section{{background:#fff;border:1px solid var(--gray-4);border-radius:12px;padding:28px;margin-bottom:24px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}}
.progress-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px;flex-wrap:wrap;gap:16px}}
.progress-title{{font-size:20px;font-weight:700;color:var(--gray-12);letter-spacing:-0.02em;margin:0}}
.progress-subtitle{{font-size:13px;color:var(--gray-10);margin-top:4px}}
.progress-badge{{background:linear-gradient(135deg,var(--green-9),var(--green-11));color:#fff;padding:12px 20px;border-radius:12px;text-align:center;box-shadow:0 2px 8px rgba(48,164,108,0.25)}}
.progress-badge-val{{font-size:28px;font-weight:700;line-height:1}}
.progress-badge-lbl{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;opacity:0.9;margin-top:2px}}
.progress-stats{{display:flex;align-items:center;justify-content:center;gap:20px;padding:20px;background:linear-gradient(135deg,var(--gray-1),var(--gray-2));border-radius:8px;margin-bottom:24px;flex-wrap:wrap}}
.progress-stat{{text-align:center}}
.progress-stat-val{{font-size:36px;font-weight:700;line-height:1;letter-spacing:-0.02em}}
.progress-stat-lbl{{font-size:11px;color:var(--gray-10);text-transform:uppercase;letter-spacing:0.5px;font-weight:600;margin-top:6px}}
.progress-arrow{{font-size:24px;color:var(--gray-8);font-weight:300}}
.chart-container{{position:relative;height:280px;margin-top:8px}}

/* ── footer ── */
.footer{{text-align:center;color:var(--gray-10);font-size:12px;margin-top:32px;padding-bottom:32px;font-weight:500}}

/* ── responsive ── */
@media (max-width: 768px) {{
  .page{{padding:16px}}
  .stats{{flex-direction:column}}
  .toolbar{{flex-direction:column;align-items:stretch}}
  .toolbar input,.toolbar select{{width:100%}}
  table{{font-size:11px}}
  th,td{{padding:6px 8px}}
}}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-logo">Sonatype <span>•</span> Documentation Health</div>
  <div class="topbar-meta">Generated {scan_ts}</div>
</div>

<div class="page">

  <div class="page-heading">
    <div>
      <h1>Broken Link Checker Report</h1>
      <div class="page-sub">Scanned: <a href="{base_url}" target="_blank">{base_url}</a></div>
    </div>
  </div>

  <div class="stats">
    <div class="stat-card tomato">
      <div class="val" id="visibleCount">{total_broken}</div>
      <div class="lbl">Total Broken</div>
    </div>
    <div class="stat-card blue">
      <div class="val">{total_cats}</div>
      <div class="lbl">Categories</div>
    </div>
    <div class="stat-card">
      <div class="val" style="font-size:13px;color:var(--gray-11);padding-top:8px">{scan_ts}</div>
      <div class="lbl">Last Scan Date</div>
    </div>
    <div class="stat-card">
      <div class="val" style="font-size:20px;color:var(--gray-12);padding-top:4px">Weekly</div>
      <div class="lbl">Mon 10 AM IST</div>
    </div>
    <div class="stat-card green">
      <div class="val">{healthy_pct}%</div>
      <div class="lbl">Link Health</div>
    </div>
  </div>

  <!-- Progress Tracking Section -->
  <div class="progress-section">
    <div class="progress-header">
      <div>
        <h2 class="progress-title">Documentation Health Progress</h2>
        <p class="progress-subtitle">Broken links trend since June 1, 2026</p>
      </div>
      <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
        <div class="progress-badge">
          <div class="progress-badge-val">{improvement_pct}%</div>
          <div class="progress-badge-lbl">Improvement</div>
        </div>
        <div class="health-score">
          <div class="health-grade {health_grade_class}">{health_grade}</div>
          <div class="health-info">
            <div class="health-label">Health Score</div>
            <div class="health-detail">{health_status}</div>
          </div>
        </div>
      </div>
    </div>

    <div class="progress-stats">
      <div class="progress-stat">
        <div class="progress-stat-val" style="color:var(--gray-11)">{total_ever_broken}</div>
        <div class="progress-stat-lbl">Total Ever Broken (Jun 1+)</div>
      </div>
      <div class="progress-arrow">−</div>
      <div class="progress-stat">
        <div class="progress-stat-val" style="color:var(--blue-11)">{links_fixed}</div>
        <div class="progress-stat-lbl">Links Fixed</div>
      </div>
      <div class="progress-arrow">=</div>
      <div class="progress-stat">
        <div class="progress-stat-val" style="color:var(--tomato-11)">{total_broken}</div>
        <div class="progress-stat-lbl">Currently Broken</div>
      </div>
    </div>

    <div class="chart-container">
      <canvas id="progressChart"></canvas>
    </div>
  </div>

  <!-- Insights: Category Breakdown + Top Pages -->
  <div class="insights-grid">
    <div class="insight-card">
      <div class="insight-title">Broken Links by Category</div>
      <div class="donut-wrap">
        <div class="donut-container">
          <canvas id="categoryChart"></canvas>
          <div class="donut-center">
            <div class="donut-center-val">{total_broken}</div>
            <div class="donut-center-lbl">Total</div>
          </div>
        </div>
        <ul class="donut-legend" id="donutLegend"></ul>
      </div>
    </div>
    <div class="insight-card">
      <div class="insight-title">Top Pages Needing Attention</div>
      <ol class="top-pages-list">{top_pages_html}</ol>
    </div>
  </div>

  <div class="toolbar">
    <input type="text" id="searchBox" placeholder="Search URL, source or context…" oninput="applyFilters()">
    <div class="sep"></div>
    <select id="catFilter" onchange="applyFilters()">
      <option value="">All Categories</option>
    </select>
    <select id="errFilter" onchange="applyFilters()">
      <option value="">All Errors</option>
      <option value="404">404</option>
      <option value="Connection">Connection</option>
      <option value="Timeout">Timeout</option>
      <option value="SSL">SSL</option>
      <option value="Invalid URL">Invalid URL</option>
    </select>
    <button class="btn-clear" onclick="clearFilters()">Clear</button>
    <div class="count" id="countLabel">{total_broken} results</div>
  </div>

  <div class="tbl-wrap">
    <table>
      <thead>
        <tr>
          <th class="col-cat">Category</th>
          <th class="col-src">Source Page</th>
          <th class="col-url">Broken URL</th>
          <th class="col-ctx">Context</th>
          <th class="col-err">Error</th>
        </tr>
      </thead>
      <tbody id="tableBody"></tbody>
    </table>
    <div class="empty" id="emptyState" style="display:none">
      <div class="ico">🔍</div>
      <p>No results match your filters.</p>
    </div>
  </div>

  <div class="footer">Sonatype Documentation Health Check &mdash; {scan_ts}</div>
</div>

<script>
const ROWS = {rows_json};
const CATS = {cats_json};
const PROGRESS_DATA = {progress_json};
const CATEGORY_DATA = {category_json};

// Render progress chart
if (typeof Chart !== 'undefined' && PROGRESS_DATA.labels.length > 0) {{
  const ctx = document.getElementById('progressChart').getContext('2d');
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: PROGRESS_DATA.labels,
      datasets: [{{
        label: 'Broken Links',
        data: PROGRESS_DATA.counts,
        borderColor: '#006adc',
        backgroundColor: 'rgba(0, 106, 220, 0.1)',
        borderWidth: 3,
        fill: true,
        tension: 0.35,
        pointBackgroundColor: '#d95030',
        pointBorderColor: '#fff',
        pointBorderWidth: 2,
        pointRadius: 6,
        pointHoverRadius: 8,
        pointHoverBackgroundColor: '#be2c10',
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#1c1c24',
          titleFont: {{ family: 'Inter', size: 13, weight: '600' }},
          bodyFont: {{ family: 'Inter', size: 13 }},
          padding: 12,
          cornerRadius: 6,
          displayColors: false,
          callbacks: {{
            label: function(context) {{
              return context.parsed.y + ' broken links';
            }}
          }}
        }}
      }},
      scales: {{
        y: {{
          beginAtZero: true,
          grid: {{ color: '#f0f0f3', drawBorder: false }},
          ticks: {{
            font: {{ family: 'Inter', size: 12 }},
            color: '#6e6e7c',
            padding: 8
          }}
        }},
        x: {{
          grid: {{ display: false }},
          ticks: {{
            font: {{ family: 'Inter', size: 12 }},
            color: '#6e6e7c',
            padding: 8
          }}
        }}
      }}
    }}
  }});
}}

// Render category donut chart (Sonatype grayscale palette - clean, minimal)
if (typeof Chart !== 'undefined' && CATEGORY_DATA.labels.length > 0) {{
  const donutColors = ['#006adc','#4a4a56','#8b8b9a','#bbbbc6','#d9d9e0','#e8e8ec'];
  const ctxCat = document.getElementById('categoryChart').getContext('2d');
  new Chart(ctxCat, {{
    type: 'doughnut',
    data: {{
      labels: CATEGORY_DATA.labels,
      datasets: [{{
        data: CATEGORY_DATA.counts,
        backgroundColor: donutColors,
        borderColor: '#fff',
        borderWidth: 2,
        hoverOffset: 6
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      cutout: '68%',
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#1c1c24',
          titleFont: {{ family: 'Inter', size: 12, weight: '600' }},
          bodyFont: {{ family: 'Inter', size: 12 }},
          padding: 10,
          cornerRadius: 6,
          displayColors: false,
          callbacks: {{
            label: function(context) {{
              const total = context.dataset.data.reduce((a,b) => a+b, 0);
              const pct = ((context.parsed / total) * 100).toFixed(0);
              return context.parsed + ' links (' + pct + '%)';
            }}
          }}
        }}
      }}
    }}
  }});

  // Build custom legend
  const legendEl = document.getElementById('donutLegend');
  CATEGORY_DATA.labels.forEach((label, i) => {{
    const li = document.createElement('li');
    li.className = 'donut-legend-item';
    li.innerHTML = '<span class="donut-legend-dot" style="background:' + donutColors[i] + '"></span>' +
                   '<span class="donut-legend-name">' + label + '</span>' +
                   '<span class="donut-legend-count">' + CATEGORY_DATA.counts[i] + '</span>';
    legendEl.appendChild(li);
  }});
}}

// Populate category filter
const catSel = document.getElementById('catFilter');
CATS.forEach(c => {{
  const o = document.createElement('option');
  o.value = c; o.textContent = c;
  catSel.appendChild(o);
}});

function renderRow(r) {{
  const srcShort = r.src.replace(/https?:\/\/[^/]+/, '').replace(/\.html$/, '') || r.src;
  return `<tr data-cat="${{r.cat}}" data-err="${{r.err}}">
    <td><span class="cat-pill">${{r.cat}}</span></td>
    <td><a class="src-link" href="${{r.src}}" target="_blank" title="${{r.src}}">${{srcShort}}</a></td>
    <td><span class="broken-url" title="${{r.url}}">${{r.url}}</span></td>
    <td><span class="ctx-text">${{r.ctx || '<em style="color:var(--gray-9)">—</em>'}}</span></td>
    <td><span class="badge ${{r.ecls}}">${{r.err}}</span></td>
  </tr>`;
}}

function applyFilters() {{
  const q = document.getElementById('searchBox').value.toLowerCase();
  const cat = document.getElementById('catFilter').value;
  const err = document.getElementById('errFilter').value;

  let visible = 0;
  const body = document.getElementById('tableBody');
  body.innerHTML = '';
  const frag = document.createDocumentFragment();

  ROWS.forEach(r => {{
    if (cat && r.cat !== cat) return;
    if (err && r.err !== err) return;
    if (q && !r.url.toLowerCase().includes(q) && !r.src.toLowerCase().includes(q) && !r.ctx.toLowerCase().includes(q)) return;
    const tr = document.createElement('tbody');
    tr.innerHTML = renderRow(r);
    frag.appendChild(tr.firstChild);
    visible++;
  }});

  body.appendChild(frag);
  document.getElementById('visibleCount').textContent = visible;
  document.getElementById('countLabel').textContent = visible + ' result' + (visible !== 1 ? 's' : '');
  document.getElementById('emptyState').style.display = visible === 0 ? 'block' : 'none';
}}

function clearFilters() {{
  document.getElementById('searchBox').value = '';
  document.getElementById('catFilter').value = '';
  document.getElementById('errFilter').value = '';
  applyFilters();
}}

applyFilters();
</script>
</body>
</html>
"""

        with open(html_file, 'w', encoding='utf-8') as f:
            f.write(html_content)

        self.logger.info(f"✓ HTML report saved: {html_file}")
        return html_file


# ============================================================================
# GITHUB PAGES UPLOADER
# ============================================================================

class GitHubPagesUploader:
    """Upload reports to GitHub Pages and get public URL."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.repo_name = config.github_repo_name
        self.username = config.github_username or self._get_github_username()

    def _get_github_username(self) -> str:
        """Get GitHub username from gh CLI."""
        try:
            result = subprocess.run(
                ['gh', 'api', 'user', '--jq', '.login'],
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout.strip()
        except Exception as e:
            self.logger.error(f"Failed to get GitHub username: {e}")
            return ""

    def upload_report(self, html_file: Path, csv_file: Path) -> Optional[str]:
        """
        Upload report to GitHub Pages and return public URL.

        Returns:
            Public URL of the uploaded report, or None if upload failed
        """
        if not self.config.enable_github_upload:
            self.logger.info("GitHub upload disabled in config")
            return None

        if not self.username:
            self.logger.error("GitHub username not configured")
            return None

        try:
            self.logger.info(f"📤 Uploading report to GitHub Pages...")

            # Clone or update repo
            repo_dir = Path(__file__).parent / '.github_pages_repo'

            # Use GH_PAT token if available (for GitHub Actions)
            github_token = os.getenv('GITHUB_TOKEN')
            if github_token:
                repo_url = f"https://x-access-token:{github_token}@github.com/{self.username}/{self.repo_name}.git"
            else:
                repo_url = f"https://github.com/{self.username}/{self.repo_name}.git"

            if repo_dir.exists():
                self.logger.info("Updating existing repository...")
                subprocess.run(['git', '-C', str(repo_dir), 'pull'], check=True, capture_output=True)
            else:
                self.logger.info("Cloning repository...")
                subprocess.run(['git', 'clone', repo_url, str(repo_dir)], check=True, capture_output=True)

            # Copy files to repo
            import shutil
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

            # Copy HTML report as latest.html
            shutil.copy(html_file, repo_dir / 'latest.html')

            # Also save timestamped version
            archive_dir = repo_dir / 'archive'
            archive_dir.mkdir(exist_ok=True)
            shutil.copy(html_file, archive_dir / f'report_{timestamp}.html')
            shutil.copy(csv_file, archive_dir / f'report_{timestamp}.csv')

            # Update index.html with last updated time
            index_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="0; url=./latest.html">
    <title>Broken Links Reports</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: linear-gradient(to bottom, #ffffff, #f5f3ff);
        }}
        .container {{
            text-align: center;
            padding: 40px;
            background: white;
            border-radius: 12px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }}
        h1 {{ color: #6B46C1; }}
        p {{ color: #666; font-size: 1.1em; margin: 10px 0; }}
        a {{ color: #6B46C1; text-decoration: none; font-weight: 600; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔗 Broken Links Reports</h1>
        <p>Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p>Redirecting to <a href="./latest.html">latest report</a>...</p>
        <p><a href="./archive/">View archive</a></p>
    </div>
</body>
</html>"""

            with open(repo_dir / 'index.html', 'w') as f:
                f.write(index_content)

            # Create archive index
            archive_files = sorted([f for f in archive_dir.glob('*.html')], reverse=True)
            archive_index = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Report Archive</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            max-width: 800px;
            margin: 40px auto;
            padding: 20px;
            background: linear-gradient(to bottom, #ffffff, #f5f3ff);
        }
        h1 { color: #6B46C1; }
        ul { list-style: none; padding: 0; }
        li {
            background: white;
            margin: 10px 0;
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        a { color: #6B46C1; text-decoration: none; font-weight: 600; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h1>📚 Report Archive</h1>
    <p><a href="../">← Back to latest</a></p>
    <ul>
"""
            for html_report in archive_files:
                report_name = html_report.stem
                archive_index += f'        <li><a href="./{html_report.name}">📄 {report_name}</a></li>\n'

            archive_index += """    </ul>
</body>
</html>"""

            with open(archive_dir / 'index.html', 'w') as f:
                f.write(archive_index)

            # Commit and push
            subprocess.run(['git', '-C', str(repo_dir), 'add', '.'], check=True, capture_output=True)
            subprocess.run(
                ['git', '-C', str(repo_dir), 'commit', '-m', f'Update report: {timestamp}'],
                capture_output=True
            )
            subprocess.run(['git', '-C', str(repo_dir), 'push'], check=True, capture_output=True)

            # Construct public URL
            public_url = f"https://{self.username}.github.io/{self.repo_name}/"

            self.logger.info(f"✅ Report uploaded successfully!")
            self.logger.info(f"🌐 Public URL: {public_url}")

            return public_url

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Git command failed: {e}")
            self.logger.error(f"Output: {e.stderr if hasattr(e, 'stderr') else 'N/A'}")
            return None
        except Exception as e:
            self.logger.error(f"Failed to upload to GitHub Pages: {e}")
            return None


# ============================================================================
# SLACK NOTIFIER
# ============================================================================

class SlackNotifier:
    """Send Slack notifications with report summary and link."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger

    def send_report_notification(self, broken_links: List[Dict], public_url: str, by_category: Dict[str, List[Dict]], total_checked: int = 0):
        """Send Slack notification with report summary and public URL."""
        if not self.config.enable_slack or not self.config.slack_webhook:
            self.logger.info("Slack notifications disabled or webhook not configured")
            return

        try:
            total_broken = len(broken_links)
            scan_date = datetime.now().strftime('%Y-%m-%d')

            # Category bullet list (no emojis)
            cat_lines = ""
            for category in sorted(by_category.keys()):
                count = len(by_category[category])
                # Simplify category names for cleaner display
                cat_name = category.replace('Sonatype ', '').replace('Nexus Repository', 'Nexus Repository')
                cat_lines += f"• {cat_name}: {count}\n"

            # Key issues — identify patterns in broken links
            from collections import Counter
            domain_counts = Counter()

            for lnk in broken_links:
                url = lnk.get('url', '')

                # Track which domains/paths have issues
                if 'jenkins' in url.lower() or 'jfrog' in url.lower():
                    domain_counts['Jenkins & JFrog docs (404s)'] += 1
                elif 'support.sonatype.com' in url:
                    domain_counts['Sonatype Support articles'] += 1
                elif 'npmjs.com' in url:
                    domain_counts['npm documentation'] += 1
                elif 'github.com' in url:
                    domain_counts['GitHub references'] += 1
                elif 'http://https://' in url:
                    domain_counts['Malformed URLs (double protocol)'] += 1
                else:
                    domain_counts['Other broken links'] += 1

            # Get top issue domains
            if domain_counts:
                key_issues = ', '.join(f"{k}" for k, v in domain_counts.most_common(3))
            else:
                key_issues = "No specific patterns detected"

            checked_str = f"{total_checked:,}" if total_checked else "—"

            message = {
                "channel": self.config.slack_channel if self.config.slack_channel else None,
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "Hi Team,\n\n"
                                "Just sharing this week's broken links scan results. "
                                "Whenever you get a chance, could you take a quick look at the links in your respective areas? "
                                "No rush — just something to keep on the radar as we continue to keep our docs in great shape."
                            )
                        }
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{checked_str} links checked | {total_broken} broken found"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*By Category:*\n{cat_lines.strip()}"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"<{public_url}|View detailed report>"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Key Issues:* {key_issues}"
                        }
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Thanks so much for all you do! Feel free to reach out if you have any questions or need help investigating anything."
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"Scan date: {scan_date}"
                            }
                        ]
                    }
                ]
            }

            # Send to Slack
            response = requests.post(
                self.config.slack_webhook,
                json=message,
                headers={'Content-Type': 'application/json'},
                timeout=10
            )

            if response.status_code == 200:
                self.logger.info("✅ Slack notification sent successfully!")
            else:
                self.logger.error(f"Failed to send Slack notification: {response.status_code} {response.text}")

        except Exception as e:
            self.logger.error(f"Error sending Slack notification: {e}")

    def _get_category_emoji(self, category: str) -> str:
        """Get emoji for category (9 official categories only)."""
        emoji_map = {
            'Sonatype Platform Overview': '🏠',
            'Sonatype Guide': '📖',
            'Sonatype Nexus Repository': '📦',
            'Sonatype IQ Server': '🔍',
            'Sonatype Lifecycle': '🔄',
            'Sonatype Repository Firewall': '🛡️',
            'Sonatype SBOM Manager': '📋',
            'Sonatype Developer': '💻',
            'Sonatype Integrations': '🔗',
        }
        return emoji_map.get(category, '📦')


# ============================================================================
# ENHANCED MONITOR
# ============================================================================

class EnhancedBrokenLinkMonitor:
    """Enhanced broken link monitoring with categories."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.link_checker = EnhancedLinkChecker(config, logger)
        self.report_gen = EnhancedReportGenerator(config, logger)
        self.github_uploader = GitHubPagesUploader(config, logger)
        self.slack_notifier = SlackNotifier(config, logger)

    def check_all_sites(self) -> Tuple[List[Dict], List[Dict]]:
        """
        Check all sites and return broken links.

        Returns:
            Tuple of (truly_broken_links, false_positives)
        """
        all_broken = []
        all_false_positives = []
        # Track ALL unique links scanned (for full audit CSV)
        self.all_scanned_links: List[Dict] = []

        for doc_url in self.config.docs_urls:
            self.logger.info("="*70)
            self.logger.info(f"Checking: {doc_url}")
            self.logger.info("="*70)

            crawler = WebCrawler(self.config, self.logger, self.link_checker)
            all_links = crawler.crawl_site(doc_url)

            self.logger.info(f"\n🔍 Checking links with strict validation ({self.config.parallel_workers} workers)...")

            links_to_check = []
            seen_links = set()  # dedupe across all source pages
            for source_url, links_dict in all_links.items():
                for link, context in links_dict.items():
                    # Skip excluded patterns
                    excluded = False
                    for pattern in self.config.exclude_patterns:
                        if re.search(pattern, link):
                            excluded = True
                            break

                    if excluded:
                        if link not in seen_links:
                            self.logger.info(f"EXCLUDED: {link}")
                            seen_links.add(link)
                    elif link not in seen_links and link not in self.link_checker.checked_urls:
                        seen_links.add(link)
                        links_to_check.append((link, source_url, context))

                    if self.config.max_links > 0 and len(links_to_check) >= self.config.max_links:
                        break
                if self.config.max_links > 0 and len(links_to_check) >= self.config.max_links:
                    break

            total_to_check = len(links_to_check)
            checked_count = 0

            with ThreadPoolExecutor(max_workers=self.config.parallel_workers) as executor:
                future_to_link = {
                    executor.submit(self.link_checker.check_link_with_retry, link): (link, source_url, context)
                    for link, source_url, context in links_to_check
                }

                for future in as_completed(future_to_link):
                    link, source_url, context = future_to_link[future]
                    checked_count += 1

                    if checked_count % 20 == 0 or checked_count == total_to_check:
                        self.logger.info(f"✓ Checked {checked_count}/{total_to_check} links...")

                    try:
                        status, error = future.result()

                        # Record ALL scanned links (working + broken) for full audit
                        self.all_scanned_links.append({
                            'url': link,
                            'source': source_url,
                            'status': status,
                            'error': error if error else '',
                            'site': doc_url,
                            'context': context,
                            'result': 'BROKEN' if (link.startswith('https://help-test.sonatype.com/') or (not (status > 0 and status < 400) and self.link_checker.is_truly_broken(link, status, error))) else ('OK' if (status > 0 and status < 400) else 'FALSE_POSITIVE')
                        })

                        # Force-broken links: flag regardless of HTTP status
                        force_broken = link.startswith('https://help-test.sonatype.com/')

                        if force_broken or status == 0 or status >= 400:
                            link_data = {
                                'url': link,
                                'source': source_url,
                                'status': status,
                                'error': error if error else ('Test environment URL in production docs' if force_broken else ''),
                                'site': doc_url,
                                'context': context
                            }

                            # Classify as truly broken or false positive
                            if force_broken or self.link_checker.is_truly_broken(link, status, error):
                                all_broken.append(link_data)
                                self.logger.warning(f"❌ BROKEN: {link} ({error})")
                            else:
                                all_false_positives.append(link_data)
                                self.logger.info(f"⚠️  FALSE POSITIVE: {link} ({error})")

                    except Exception as e:
                        self.logger.error(f"Error checking {link}: {e}")

        # Clean up session connections to free file descriptors
        self.link_checker.session.close()
        self.logger.info("✓ Closed HTTP session to free resources")

        return all_broken, all_false_positives

    def _save_all_links_csv(self, all_links: List[Dict]) -> Path:
        """Save comprehensive CSV of ALL scanned links (working + broken + false positives).

        Provides a complete audit log and proves the deduplication is working.
        """
        import csv as _csv
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_file = self.config.report_dir / f"all_scanned_links_{timestamp}.csv"

        # Deduplicate by URL (final safety check)
        seen_urls = set()
        unique_links = []
        for link in all_links:
            url = link.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_links.append(link)

        # Sort: broken first, then false positives, then OK
        result_order = {'BROKEN': 0, 'FALSE_POSITIVE': 1, 'OK': 2}
        unique_links.sort(key=lambda x: (result_order.get(x.get('result', 'OK'), 3), x.get('url', '')))

        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = _csv.writer(f)
            writer.writerow(['URL', 'Source Page', 'Status', 'Result', 'Error', 'Context'])
            for link in unique_links:
                writer.writerow([
                    link.get('url', ''),
                    link.get('source', ''),
                    link.get('status', ''),
                    link.get('result', 'OK'),
                    link.get('error', ''),
                    link.get('context', ''),
                ])

        # Log stats for transparency
        total = len(all_links)
        unique = len(unique_links)
        duplicates = total - unique
        by_result = defaultdict(int)
        for link in unique_links:
            by_result[link.get('result', 'OK')] += 1

        self.logger.info(f"✓ All-links audit CSV saved: {csv_file}")
        self.logger.info(f"  Total processed: {total} | Unique: {unique} | Duplicates removed: {duplicates}")
        self.logger.info(f"  OK: {by_result['OK']} | Broken: {by_result['BROKEN']} | False positives: {by_result['FALSE_POSITIVE']}")
        return csv_file

    def run_check(self):
        """Run enhanced check with categorization."""
        # Increase system limits for file descriptors to handle many connections
        try:
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            new_limit = min(hard, 65536)  # Increase to max allowed
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_limit, hard))
            self.logger.info(f"Increased file descriptor limit from {soft} to {new_limit}")
        except Exception as e:
            self.logger.warning(f"Could not increase file descriptor limit: {e}")

        self.logger.info("="*70)
        self.logger.info("🚀 Enhanced Broken Link Checker with Categories")
        self.logger.info("="*70)
        self.logger.info(f"Max retries per link: {self.config.max_retries}")
        self.logger.info(f"Retry backoff factor: {self.config.retry_backoff}")
        self.logger.info("="*70)

        start_time = time.time()

        truly_broken, false_positives = self.check_all_sites()

        elapsed = time.time() - start_time
        total_checked = len(self.link_checker.checked_urls)

        self.logger.info("="*70)
        self.logger.info("✅ Check Complete")
        self.logger.info("="*70)
        self.logger.info(f"Total links checked: {total_checked}")
        self.logger.info(f"Truly broken links: {len(truly_broken)}")
        self.logger.info(f"False positives (403, local URLs, etc.): {len(false_positives)}")
        self.logger.info(f"Time elapsed: {elapsed:.1f} seconds")
        self.logger.info("="*70)

        # Strip any excluded-pattern URLs from broken list before reporting
        if self.config.exclude_patterns:
            import re as _re
            truly_broken = [
                lnk for lnk in truly_broken
                if not any(_re.search(pat, lnk['url']) for pat in self.config.exclude_patterns)
            ]

        # Save comprehensive all-links CSV (audit log of every scanned URL)
        all_links = getattr(self.link_checker, 'all_scanned_links', None) or getattr(self, 'all_scanned_links', [])
        if not all_links and hasattr(self, 'all_scanned_links'):
            all_links = self.all_scanned_links
        if all_links:
            self._save_all_links_csv(all_links)

        # Generate categorized CSV and HTML reports
        if truly_broken:
            base_url = self.config.docs_urls[0] if self.config.docs_urls else ""
            csv_file = self.report_gen.generate_csv_with_categories(truly_broken, base_url)
            html_file = self.report_gen.generate_html_report(truly_broken, base_url)

            # Generate category summary
            by_category = self.report_gen.generate_category_summary(truly_broken, base_url)

            self.logger.info("\n📊 Broken Links by Category:")
            for category in sorted(by_category.keys()):
                count = len(by_category[category])
                display_cat = CategoryExtractor.get_display_category(category)
                self.logger.info(f"  {display_cat}: {count} broken links")

            # Upload to GitHub Pages
            public_url = self.github_uploader.upload_report(html_file, csv_file)

            # Send Slack notification with public URL
            if public_url:
                self.slack_notifier.send_report_notification(truly_broken, public_url, by_category, total_checked)

            return csv_file, html_file, by_category, public_url

        return None, None, {}, None


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point."""
    logger = setup_logging()
    config = Config()

    if not config.validate():
        sys.exit(1)

    monitor = EnhancedBrokenLinkMonitor(config, logger)
    csv_file, html_file, by_category, public_url = monitor.run_check()

    if csv_file:
        print(f"\n✅ CSV report: {csv_file}")
        print(f"✅ HTML report: {html_file}")
        if public_url:
            print(f"🌐 Public URL: {public_url}")
        print(f"\n📁 Categories found: {len(by_category)}")


if __name__ == '__main__':
    main()
