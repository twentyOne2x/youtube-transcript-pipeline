from src.utils import root_directory

AUTHORS = ['Kyle Samani', 'Threadguy']

FIRMS = ['kamino', 'meteora', 'bloxroute', 'sol', 'solana', 'helius', 'raydium', 'jito', 'orca', 'jupiter',
         'pump.fun', 'bonkbot', 'banana gun', 'axiom', 'photon', 'svm',
         'a16z','paradigm', 'anza']


# Note, the use of keywords List is an attempt at filtering YouTube videos by name content to reduce noise
KEYWORDS_TO_INCLUDE = ['perps', 'derivatives', 'options', 'order flow', 'orderflow', 'transaction', 'mev', 'ordering', 'dex', 'front-running', 'arbitrage', 'back-running',
            'maximal extractable value', 'trading games', 'timing games', 'arbitrage games', 'timing', 'on-chain games',
            'fees', 'defi', 'latency', 'market design', 'searcher', 'staking','market microstructure','telegram bot',
            'cow', 'liquidity', 'censorship', 'ofa', 'pfof', 'payment for order flow', 'decentralisation', 'decentralization', "incentive", "incentives", 'hyperliquid',
            'auction', 'mechanism design', 'Price-of-Anarchy', 'protocol economics', 'pools', 'firedancer'
           ]

KEYWORDS_TO_INCLUDE += AUTHORS
KEYWORDS_TO_INCLUDE += FIRMS

KEYWORDS_TO_EXCLUDE = ['#short', '#shorts']

YOUTUBE_VIDEOS_CSV_FILE_PATH = f"{root_directory()}/data/links/youtube/youtube_videos.csv"
