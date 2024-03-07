# -*- coding: utf-8 -*-
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from datetime import datetime, timedelta
import re
import json
import time
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
import logging
from config import TWITTER_AUTH_TOKEN


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class TwitterExtractor:
    def __init__(self, headless=True):
        self.driver = self._start_chrome(headless)
        self.set_token()

    def _start_chrome(self, headless):
        options = Options()
        # options.headless = headless
        # driver = webdriver.Chrome(options=options)
        driver = webdriver.Chrome(options=options,executable_path="/Users/a0000/mywork/chromedriver")
        driver.get("https://twitter.com")
        return driver

    def set_token(self, auth_token=TWITTER_AUTH_TOKEN):
        if not auth_token or auth_token == "YOUR_TWITTER_AUTH_TOKEN_HERE":
            raise ValueError("缺少访问令牌，请正确配置。")
        expiration = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        cookie_script = f"document.cookie = 'auth_token={auth_token}; expires={expiration}; path=/';"
        self.driver.execute_script(cookie_script)

    def fetch_tweets(self, page_url, start_date, end_date):
        self.driver.get(page_url)
        cur_filename = f"data/tweets_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"

        # 将 start_date 和 end_date 从 "YYYY-MM-DD" 转换为 datetime 对象
        start_date = datetime.strptime(start_date, "%Y-%m-%d")
        end_date = datetime.strptime(end_date, "%Y-%m-%d")

        while True:
            tweet = self._get_first_tweet()
            if not tweet:
                continue

            row = self._process_tweet(tweet)
            print(row['text'])
            print("=======")
            if row["date"]:
                try:
                    date = datetime.strptime(row["date"], "%Y-%m-%d")
                except ValueError as e:
                    # 推断日期格式
                    logger.info(
                        f"日期格式错误，尝试另一种格式。{row['date']}",
                        e,
                    )
                    date = datetime.strptime(row["date"], "%d/%m/%Y")

                if date < start_date:
                    break
                elif date > end_date:
                    self._delete_first_tweet()
                    continue

            self._save_to_json(row, filename=f"{cur_filename}.json")
            logger.info(
                f"保存推文...\n{row['date']},  {row['author_name']} -- {row['text'][:50]}...\n\n"
            )
            self._delete_first_tweet()

        # 保存到 Excel
        self._save_to_excel(
            json_filename=f"{cur_filename}.json", output_filename=f"{cur_filename}.xlsx"
        )

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(TimeoutException),
    )
    def _get_first_tweet(
        self, timeout=10, use_hacky_workaround_for_reloading_issue=True
    ):
        try:
            # 等待推文或错误消息出现
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.find_elements(By.XPATH, "//article[@data-testid='tweet']")
                or d.find_elements(By.XPATH, "//span[contains(text(),'Try reloading')]")
            )

            # 检查错误消息并尝试点击“重试”
            error_message = self.driver.find_elements(
                By.XPATH, "//span[contains(text(),'Try reloading')]"
            )
            if error_message and use_hacky_workaround_for_reloading_issue:
                logger.info(
                    "遇到 '出了点问题，请尝试重新加载' 错误。\n尝试使用一个不太优雅的解决方法（点击另一个选项卡然后切换回来）。请注意，这并不是最佳解决方案。\n"
                )
                logger.info(
                    "您无需担心数据重复。保存到 Excel 部分会去重。"
                )
                self._navigate_tabs()

                WebDriverWait(self.driver, timeout).until(
                    lambda d: d.find_elements(
                        By.XPATH, "//article[@data-testid='tweet']"
                    )
                )
            elif error_message and not use_hacky_workaround_for_reloading_issue:
                raise TimeoutException(
                    "存在错误消息。不使用不太优雅的解决方法。"
                )

            else:
                # 如果没有错误消息，则假定推文存在
                return self.driver.find_element(
                    By.XPATH, "//article[@data-testid='tweet']"
                )

        except TimeoutException:
            logger.error("等待推文或点击 '重试' 超时")
            raise
        except NoSuchElementException:
            logger.error("无法找到推文或 '重试' 按钮")
            raise

    def _navigate_tabs(self, target_tab="Likes"):
        # 处理 '重试' 问题。不太优雅。
        try:
            # 点击 'Media' 选项卡
            self.driver.find_element(By.XPATH, "//span[text()='Media']").click()
            time.sleep(2)  # 等待 Media 选项卡加载

            # 点击回到目标选项卡。如果您要获取帖子，可以点击 'Posts' 选项卡
            self.driver.find_element(By.XPATH, f"//span[text()='{target_tab}']").click()
            time.sleep(2)  # 等待 Likes 选项卡重新加载
        except NoSuchElementException as e:
            logger.error("导航选项卡时出错：" + str(e))

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
    def _process_tweet(self, tweet):

        author_name, author_handle = self._extract_author_details(tweet)
        try:
            data = {
                "text": self._get_element_text(
                    tweet, ".//div[@data-testid='tweetText']"
                ),
                "author_name": author_name,
                "author_handle": author_handle,
                "date": self._get_element_attribute(tweet, "time", "datetime")[:10],
                "lang": self._get_element_attribute(
                    tweet, "div[data-testid='tweetText']", "lang"
                ),
                "url": self._get_tweet_url(tweet),
                "mentioned_urls": self._get_mentioned_urls(tweet),
                "is_retweet": self.is_retweet(tweet),
                "media_type": self._get_media_type(tweet),
                "images_urls": (
                    self._get_images_urls(tweet)
                    if self._get_media_type(tweet) == "Image"
                    else None
                ),
            }
        except Exception as e:
            logger.error(f"处理推文时出错：{e}")
            logger.info(f"推文：{tweet}")
            raise
        # 转换日期格式
        if data["date"]:
            data["date"] = datetime.strptime(data["date"], "%Y-%m-%d").strftime(
                "%Y-%m-%d"
            )

        # 从 aria-label 中提取数字
        data.update(
            {
                "num_reply": self._extract_number_from_aria_label(tweet, "reply"),
                "num_retweet": self._extract_number_from_aria_label(tweet, "retweet"),
                "num_like": self._extract_number_from_aria_label(tweet, "like"),
            }
        )
        return data

    def _get_element_text(self, parent, selector):
        try:
            return parent.find_element(By.XPATH, selector).text
        except NoSuchElementException:
            return ""

    def _get_element_attribute(self, parent, selector, attribute):
        try:
            return parent.find_element(By.CSS_SELECTOR, selector).get_attribute(
                attribute
            )
        except NoSuchElementException:
            return ""

    def _get_mentioned_urls(self, tweet):
        try:
            # 查找所有可能包含链接的 'a' 标签。您可能需要根据实际结构调整选择器。
            link_elements = tweet.find_elements(
                By.XPATH, ".//a[contains(@href, 'http')]"
            )
            urls = [elem.get_attribute("href") for elem in link_elements]
            return urls
        except NoSuchElementException:
            return []

    def is_retweet(self, tweet):
        try:
            # 这只是一个示例；实际结构可能有所不同。
            retweet_indicator = tweet.find_element(
                By.XPATH, ".//div[contains(text(), 'Retweeted')]"
            )
            if retweet_indicator:
                return True
        except NoSuchElementException:
            return False

    def _get_tweet_url(self, tweet):
        try:
            link_element = tweet.find_element(
                By.XPATH, ".//a[contains(@href, '/status/')]"
            )
            return link_element.get_attribute("href")
        except NoSuchElementException:
            return ""

    def _extract_author_details(self, tweet):
        author_details = self._get_element_text(
            tweet, ".//div[@data-testid='User-Name']"
        )
        # 通过换行符拆分字符串
        parts = author_details.split("\n")
        if len(parts) >= 2:
            author_name = parts[0]
            author_handle = parts[1]
        else:
            # 如果格式不符合预期，则回退
            author_name = author_details
            author_handle = ""

        return author_name, author_handle

    def _get_media_type(self, tweet):
        if tweet.find_elements(By.CSS_SELECTOR, "div[data-testid='videoPlayer']"):
            return "Video"
        if tweet.find_elements(By.CSS_SELECTOR, "div[data-testid='tweetPhoto']"):
            return "Image"
        return "No media"

    def _get_images_urls(self, tweet):
        images_urls = []
        images_elements = tweet.find_elements(
            By.XPATH, ".//div[@data-testid='tweetPhoto']//img"
        )
        for image_element in images_elements:
            images_urls.append(image_element.get_attribute("src"))
        return images_urls

    def _extract_number_from_aria_label(self, tweet, testid):
        try:
            text = tweet.find_element(
                By.CSS_SELECTOR, f"div[data-testid='{testid}']"
            ).get_attribute("aria-label")
            numbers = [int(s) for s in re.findall(r"\b\d+\b", text)]
            return numbers[0] if numbers else 0
        except NoSuchElementException:
            return 0

    def _delete_first_tweet(self, sleep_time_range_ms=(0, 1000)):
        try:
            tweet = self.driver.find_element(
                By.XPATH, "//article[@data-testid='tweet'][1]"
            )
            self.driver.execute_script("arguments[0].remove();", tweet)
        except NoSuchElementException:
            logger.info("无法找到要删除的第一条推文。")

    @staticmethod
    def _save_to_json(data, filename="data.json"):
        with open(filename, "a", encoding="utf-8") as file:
            json.dump(data, file)
            file.write("\n")

    @staticmethod
    def _save_to_excel(json_filename, output_filename="data/data.xlsx"):
        # 读取 JSON 数据
        cur_df = pd.read_json(json_filename, lines=True)

        # 去重并保存到 Excel
        cur_df.drop_duplicates(subset=["url"], inplace=True)
        cur_df.to_excel(output_filename, index=False)
        logger.info(
            f"\n\n保存到 {output_filename} 完成。共有 {len(cur_df)} 条唯一推文。"
        )

if __name__ == "__main__":
    scraper = TwitterExtractor()
    scraper.fetch_tweets(
        "https://twitter.com/seclink/likes",
        start_date="2024-03-01",
        end_date="2024-03-05",
    )  # 使用 YYYY-MM-DD 格式

    # 如果只想导出到 Excel，可以使用以下行
    # scraper._save_to_excel(json_filename="tweets_2024-02-01_14-30-00.json", output_filename="tweets_2024-02-01_14-30-00.xlsx")
